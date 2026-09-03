#!/usr/bin/env python3
"""
MUSAEUS — Stage: Auditor
Pre-forge LUFS audit.  Scans CATALOGUED files that have not yet been forged
and reports which are outside the target loudness window.

What it does:
  - Finds CATALOGUED archive rows where rg_tagged_at IS NULL (not yet forged)
  - Runs ffmpeg loudnorm (pass-1 analysis only) on each file
  - Flags files whose integrated LUFS or true-peak fall outside tolerance
  - Writes a summary report to MUSAEUS_RUNS_ROOT/auditor_report.txt
  - Stores per-file results in archive.auditor_lufs / archive.auditor_flagged
    (columns added on first run via _ensure_columns)
  - Logs AUDITOR_PASS / AUDITOR_FLAG event per file
  - dry_run() prints the report without updating DB or writing the file

Design decisions:
  - timeout=90 per file — large FLACs won't block the pipeline indefinitely
  - --max-files cap (default 200) prevents overnight runs from scanning
    thousands of files; set to 0 to disable
  - Graceful degradation: ffmpeg not found → stage skipped with warning

ORPHEUS equivalent: SCRIPTS/orpheus_auditor.py (which we just fixed with the
same timeout + max-files pattern)
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from ..context import RunContext, StageResult
from ..db import ensure_columns
from ..duration import TOLERANCE_SEC
from .base import BaseStage, StageError

logger = logging.getLogger(__name__)

_DEFAULT_TARGET_LUFS = -18.0
_DEFAULT_TARGET_TP = -1.0
# Single definition in musaeus/duration.py (Grey's ruling 2026-09-02:
# 2.0 everywhere; it was 1.5 in four places and 2.0 in a fifth whose
# comment cited the same rationale as one of the 1.5s).
_DEFAULT_TOLERANCE = TOLERANCE_SEC
_DEFAULT_MAX_FILES = 200
_FILE_TIMEOUT_S = 90
_COMMIT_EVERY = 50


def _ensure_columns(conn) -> None:  # type: ignore[type-arg]
    """Columns this stage owns. Mechanism shared via db.ensure_columns;
    the list stays here, next to the code that reads them."""
    ensure_columns(
        conn,
        (
            ("auditor_lufs", "REAL"),
            ("auditor_tp", "REAL"),
            ("auditor_flagged", "INTEGER DEFAULT 0"),
            ("auditor_checked_at", "TEXT"),
        ),
    )
def _ffmpeg_lufs(path: Path, target_lufs: float, target_tp: float) -> tuple[float, float]:
    """
    Run ffmpeg loudnorm pass-1 analysis.
    Returns (integrated_lufs, true_peak_dbtp).
    Raises ValueError on parse failure, subprocess.TimeoutExpired on timeout.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found in PATH")

    filter_arg = f"loudnorm=I={target_lufs}:TP={target_tp}:LRA=11:print_format=json"
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-af",
        filter_arg,
        "-f",
        "null",
        "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=_FILE_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            f"ffmpeg loudnorm timed out after {_FILE_TIMEOUT_S}s for {path.name}"
        ) from exc

    m = re.search(r"\{[^{}]+\}", res.stderr, re.DOTALL)
    if not m:
        raise ValueError(f"ffmpeg loudnorm produced no JSON for {path.name}")

    data = json.loads(m.group())
    return float(data.get("input_i", 0)), float(data.get("input_tp", 0))


class AuditorStage(BaseStage):
    """
    Auditor — LUFS pre-forge audit.
    Checks integrated loudness of unforged CATALOGUED files.
    """

    NAME = "auditor"

    # ── Validate ──────────────────────────────────────────────────────────────

    def validate(self, ctx: RunContext) -> None:
        if not shutil.which("ffmpeg"):
            raise StageError("ffmpeg not found in PATH — Auditor stage requires ffmpeg.")
        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' AND rg_tagged_at IS NULL"
        ).fetchone()[0]
        logger.info("[auditor] %d unforged CATALOGUED file(s) to check", count)

    # ── Shared logic ──────────────────────────────────────────────────────────

    def _audit(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)

        # Config — can be overridden via ctx stash
        target_lufs = ctx.get("auditor_target_lufs", _DEFAULT_TARGET_LUFS)
        target_tp = ctx.get("auditor_target_tp", _DEFAULT_TARGET_TP)
        tolerance = ctx.get("auditor_tolerance", _DEFAULT_TOLERANCE)
        max_files = ctx.get("auditor_max_files", _DEFAULT_MAX_FILES)

        if not dry_run:
            _ensure_columns(ctx.conn)

        rows = ctx.conn.execute(
            """
            SELECT file_path FROM archive
            WHERE status = 'CATALOGUED'
              AND rg_tagged_at IS NULL
            ORDER BY file_path
            """
        ).fetchall()

        if max_files and len(rows) > max_files:
            logger.info("[auditor] capping %d rows to max_files=%d", len(rows), max_files)
            rows = rows[:max_files]

        passed = 0
        flagged = 0
        errors = 0
        lo = target_lufs - tolerance
        hi = target_lufs + tolerance

        from datetime import datetime, timezone

        now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

        for row in rows:
            result.files_processed += 1
            fpath = Path(row["file_path"])

            try:
                lufs, tp = _ffmpeg_lufs(fpath, target_lufs, target_tp)
            except Exception as exc:
                logger.warning("[auditor] error on %s: %s", fpath.name, exc)
                errors += 1
                result.files_skipped += 1
                result.errors.append(f"{fpath.name}: {exc}")
                continue

            is_flagged = not (lo <= lufs <= hi) or tp > target_tp

            if is_flagged:
                flagged += 1
                result.files_changed += 1
                reasons = []
                if not (lo <= lufs <= hi):
                    reasons.append(f"LUFS={lufs:.1f} outside [{lo:.1f},{hi:.1f}]")
                if tp > target_tp:
                    reasons.append(f"PEAK={tp:.1f} > {target_tp}")
                logger.info("[auditor] FLAG  %s  %s", fpath.name, "; ".join(reasons))
            else:
                passed += 1
                logger.debug("[auditor] pass  %s  LUFS=%.1f  TP=%.1f", fpath.name, lufs, tp)

            if not dry_run:
                ctx.conn.execute(
                    """
                    UPDATE archive
                       SET auditor_lufs=?,
                           auditor_tp=?,
                           auditor_flagged=?,
                           auditor_checked_at=?
                     WHERE file_path=?
                    """,
                    (lufs, tp, int(is_flagged), now, row["file_path"]),
                )
                event = "AUDITOR_FLAG" if is_flagged else "AUDITOR_PASS"
                ctx.log_event(
                    event,
                    file_path=row["file_path"],
                    new_value=f"{lufs:.2f}",
                    stage=self.NAME,
                    note=f"tp={tp:.2f}",
                )

            if result.files_processed % _COMMIT_EVERY == 0 and not dry_run:
                ctx.conn.commit()
                logger.info("[auditor] checkpoint %d", result.files_processed)

        checked = result.files_processed - result.files_skipped
        prefix = "Would check" if dry_run else "Checked"
        result.notes.append(
            f"{prefix} {checked} file(s): {passed} pass, {flagged} flag, {errors} error(s)."
        )
        if flagged:
            result.notes.append(
                f"{flagged} file(s) outside LUFS window [{lo:.1f},{hi:.1f}] "
                f"or peak > {target_tp} dBTP — run `musaeus forge` to normalise."
            )

        if not dry_run:
            self._write_report(ctx, passed, flagged, errors, target_lufs, target_tp, tolerance)

        ctx.record_stage(result)
        return result

    def _write_report(
        self,
        ctx: RunContext,
        passed: int,
        flagged: int,
        errors: int,
        target_lufs: float,
        target_tp: float,
        tolerance: float,
    ) -> None:
        """Write a text report of flagged files to RUNS_ROOT."""
        cfg = ctx.config
        report_dir = cfg.runs_root
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "auditor_report.txt"

        flagged_rows = ctx.conn.execute(
            """
            SELECT file_path, auditor_lufs, auditor_tp
            FROM archive
            WHERE auditor_flagged = 1
            ORDER BY auditor_lufs
            """
        ).fetchall()

        from datetime import datetime

        with open(report_path, "w", encoding="utf-8") as f:
            f.write("MUSAEUS AUDITOR REPORT\n")
            f.write(f"Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Target    : {target_lufs} LUFS / {target_tp} dBTP / ±{tolerance} LU\n")
            f.write("=" * 60 + "\n\n")
            f.write(f"PASSED  : {passed}\n")
            f.write(f"FLAGGED : {flagged}\n")
            f.write(f"ERRORS  : {errors}\n\n")
            if flagged_rows:
                f.write("── FLAGGED ──────────────────────────────────────────────\n")
                for row in flagged_rows:
                    name = Path(row["file_path"]).name
                    f.write(
                        f"  {name:<60}  "
                        f"LUFS={row['auditor_lufs']:>6.1f}  "
                        f"TP={row['auditor_tp']:>5.1f}\n"
                    )
        logger.info("[auditor] report written to %s", report_path)

    # ── Dry run / Run ─────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._audit(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._audit(ctx, dry_run=False)
