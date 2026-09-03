#!/usr/bin/env python3
"""
MUSAEUS — Stage: Integrity
Detect corrupt or truncated audio files using ffprobe decode-test.

What it does:
  - Finds CATALOGUED files that haven't been integrity-checked yet
  - Runs ffprobe -v error -i <file> to attempt a full decode
  - Any file that produces stderr output (errors/warnings) is flagged
  - Stores result in archive: integrity_ok (1/0) + integrity_checked_at
  - Logs INTEGRITY_FAIL event for bad files
  - Writes a report to RUNS_ROOT/integrity_report.txt
  - dry_run() reports what would be checked without writing to DB

Why this matters:
  - Silent corruption: a file that plays fine may have a corrupt header
    or truncated end — ffprobe catches this without re-encoding
  - Better than just checking file size (files can be "full-size" but corrupt)

Graceful degradation:
  - ffprobe not found → stage skipped with clear error
  - File not found on disk → skipped, GHOST will catch it
  - Timeout (>60s) → flagged as suspect

ORPHEUS equivalent: SCRIPTS/orpheus_corrupt_detector.py,
                    SCRIPTS/orpheus_integrity_check.py
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from ..context import RunContext, StageResult
from ..db import ensure_columns
from .base import BaseStage, StageError

logger = logging.getLogger(__name__)

_FFPROBE_TIMEOUT = 60
_COMMIT_EVERY = 50


# ── Column migration ──────────────────────────────────────────────────────────


def _ensure_columns(conn) -> None:  # type: ignore[type-arg]
    """Columns this stage owns. Mechanism shared via db.ensure_columns;
    the list stays here, next to the code that reads them."""
    ensure_columns(
        conn,
        (
            ("integrity_ok", "INTEGER"),
            ("integrity_checked_at", "TEXT"),
        ),
    )
def _check_file(path: str) -> tuple[bool, str]:
    """
    Run ffmpeg decode-test on a file (decode to null output).
    Returns (is_ok, error_detail).
    is_ok=True means no errors detected.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found in PATH")

    try:
        res = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-i",
                path,
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=_FFPROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"ffmpeg timed out after {_FFPROBE_TIMEOUT}s"

    stderr = res.stderr.strip()
    if stderr:
        # Truncate long error output
        detail = stderr[:300]
        return False, detail
    if res.returncode != 0:
        return False, f"ffmpeg rc={res.returncode}"
    return True, ""


# ── Stage ─────────────────────────────────────────────────────────────────────


class IntegrityStage(BaseStage):
    """
    Integrity — detect corrupt or truncated audio files via ffprobe decode-test.
    """

    NAME = "integrity"

    def validate(self, ctx: RunContext) -> None:
        if not shutil.which("ffmpeg"):
            raise StageError("ffmpeg not found in PATH — Integrity requires ffmpeg.")

        try:
            count = ctx.conn.execute(
                "SELECT COUNT(*) FROM archive "
                "WHERE status='CATALOGUED' AND integrity_checked_at IS NULL"
            ).fetchone()[0]
        except Exception:
            count = ctx.conn.execute(
                "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'"
            ).fetchone()[0]
        logger.info("[integrity] %d file(s) to check", count)

    def _check(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)

        if not dry_run:
            _ensure_columns(ctx.conn)

        max_files = ctx.get("integrity_max_files", 0)  # 0 = no cap

        try:
            rows = ctx.conn.execute(
                """
                SELECT file_path FROM archive
                WHERE status = 'CATALOGUED'
                  AND integrity_checked_at IS NULL
                ORDER BY file_path
                """
            ).fetchall()
        except Exception:
            rows = ctx.conn.execute(
                """
                SELECT file_path FROM archive
                WHERE status = 'CATALOGUED'
                ORDER BY file_path
                """
            ).fetchall()

        if max_files and len(rows) > max_files:
            logger.info("[integrity] capping %d rows to %d", len(rows), max_files)
            rows = rows[:max_files]

        from datetime import datetime, timezone

        now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

        bad_files: list[tuple[str, str]] = []
        checked = 0
        skipped = 0

        for row in rows:
            result.files_processed += 1
            fp = row["file_path"]

            if not Path(fp).exists():
                result.files_skipped += 1
                skipped += 1
                continue

            if dry_run:
                # In dry run just count — don't actually run ffprobe on everything
                checked += 1
                result.files_changed += 1
                continue

            try:
                ok, detail = _check_file(fp)
            except Exception as exc:
                logger.warning("[integrity] error checking %s: %s", fp, exc)
                result.files_skipped += 1
                result.errors.append(f"{Path(fp).name}: {exc}")
                skipped += 1
                continue

            checked += 1
            ctx.conn.execute(
                "UPDATE archive SET integrity_ok=?, integrity_checked_at=? WHERE file_path=?",
                (1 if ok else 0, now, fp),
            )
            if not ok:
                bad_files.append((fp, detail))
                result.files_changed += 1
                logger.warning("[integrity] CORRUPT: %s — %s", Path(fp).name, detail[:80])
                ctx.log_event(
                    "INTEGRITY_FAIL",
                    file_path=fp,
                    new_value="corrupt",
                    stage=self.NAME,
                    note=detail[:200],
                )
            else:
                result.files_changed += 1

            if result.files_processed % _COMMIT_EVERY == 0:
                ctx.conn.commit()
                logger.info("[integrity] checkpoint %d", result.files_processed)

        # Write report
        if not dry_run and bad_files:
            self._write_report(ctx, bad_files)

        prefix = "Would check" if dry_run else "Checked"
        result.notes.append(
            f"{prefix} {checked} file(s): {len(bad_files)} corrupt/truncated, {skipped} skipped."
        )
        if bad_files:
            result.notes.append(
                f"⚠  {len(bad_files)} corrupt file(s) flagged — "
                f"see {ctx.config.runs_root / 'integrity_report.txt'}"
            )

        ctx.record_stage(result)
        return result

    def _write_report(self, ctx: RunContext, bad_files: list[tuple[str, str]]) -> None:
        report_path = ctx.config.runs_root / "integrity_report.txt"
        ctx.config.runs_root.mkdir(parents=True, exist_ok=True)
        lines = [
            "MUSAEUS INTEGRITY REPORT",
            f"Vault  : {ctx.config.vault_root}",
            f"Found  : {len(bad_files)} corrupt/truncated file(s)",
            "=" * 72,
            "",
        ]
        for fp, detail in bad_files:
            lines.append(f"  FAIL  {fp}")
            lines.append(f"        {detail[:200]}")
            lines.append("")
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        logger.info("[integrity] report written to %s", report_path)

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._check(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._check(ctx, dry_run=False)
