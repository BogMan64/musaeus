#!/usr/bin/env python3
"""
MUSAEUS — Stage 2: Sentinel
Compute content-addressed audio hashes and detect exact duplicates.

What it does:
  - Processes all archive rows with status='PENDING' or missing audio_hash
  - Computes audio_hash (PCM stream SHA-256) and full_hash (whole-file SHA-256)
  - Updates archive with hashes and advances status to 'HASHED'
  - Detects EXACT duplicates: same audio_hash → different file_path
  - Stages duplicates in the duplicates table with type='EXACT'
  - Logs HASH_COMPUTED and DUPLICATE_FOUND events
  - dry_run() scans and reports without writing hashes or moving anything

Design:
  - Re-tagging a file: full_hash changes, audio_hash unchanged → NO duplicate
  - Same audio, different container: audio_hash matches → EXACT duplicate
  - Hash failures are logged as HASH_FAILED and file stays PENDING
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..context import RunContext, StageResult
from ..db import upsert_archive
from ..hasher import audio_hash_safe, file_hash
from .base import BaseStage

logger = logging.getLogger(__name__)


def _get_pending(conn) -> list[dict]:  # type: ignore[type-arg]
    """Return archive rows that need hashing."""
    rows = conn.execute(
        """
        SELECT file_path, audio_hash FROM archive
        WHERE status = 'PENDING'
           OR audio_hash IS NULL
        ORDER BY file_path
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _hash_group_for(conn, audio_hash_val: str) -> list[str]:
    """Return all file_paths that share the given audio_hash."""
    rows = conn.execute(
        "SELECT file_path FROM archive WHERE audio_hash = ?",
        (audio_hash_val,),
    ).fetchall()
    return [r["file_path"] for r in rows]


class SentinelStage(BaseStage):
    """
    Stage 2 — Hash audio files and stage exact duplicates.
    """

    NAME = "sentinel"

    # ── Validate ──────────────────────────────────────────────────────────────

    def validate(self, ctx: RunContext) -> None:
        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='PENDING'"
        ).fetchone()[0]
        if count == 0:
            logger.info("[sentinel] no PENDING files — stage will be a no-op")
        # ffmpeg absence is a warning, not a hard failure — file_hash still works

    # ── Dry run ───────────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=True)
        pending = _get_pending(ctx.conn)
        result.notes.append(f"Would hash {len(pending)} file(s).")

        for row in pending[:10]:
            p = Path(row["file_path"])
            result.notes.append(f"  ~ {p.name}")
        if len(pending) > 10:
            result.notes.append(f"  ... and {len(pending) - 10} more")

        result.files_processed = len(pending)
        result.files_changed = len(pending)
        ctx.record_stage(result)
        return result

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)
        pending = _get_pending(ctx.conn)

        # Track hashes we've already seen this run for duplicate detection
        seen_hashes: dict[str, str] = {}  # audio_hash → first file_path

        # Pre-load existing hashes from DB into seen_hashes
        existing = ctx.conn.execute(
            "SELECT file_path, audio_hash FROM archive WHERE audio_hash IS NOT NULL"
        ).fetchall()
        for row in existing:
            h = row["audio_hash"]
            if h not in seen_hashes:
                seen_hashes[h] = row["file_path"]

        _COMMIT_EVERY = 50  # commit progress incrementally

        for row in pending:
            path_str = row["file_path"]
            path = Path(path_str)
            result.files_processed += 1

            if not path.exists():
                logger.warning("Missing file (stale DB record — removing): %s", path_str)
                ctx.conn.execute(
                    "DELETE FROM archive WHERE file_path = ?", (path_str,)
                )
                result.files_errored += 1
                result.errors.append(f"Missing file, removed from archive: {path_str}")
                continue

            # Full-file hash (always, cheap)
            try:
                fh = file_hash(path)
            except OSError as exc:
                result.files_errored += 1
                result.errors.append(f"full_hash OS error: {path.name}: {exc}")
                logger.warning("full_hash failed: %s — %s", path.name, exc)
                continue

            # Audio-stream hash (may fail if ffmpeg absent)
            ah, err = audio_hash_safe(path)
            if err:
                ctx.log_event(
                    "HASH_FAILED",
                    file_path=path_str,
                    stage=self.NAME,
                    note=err,
                )
                # Still update full_hash so change-detection works
                upsert_archive(ctx.conn, {"file_path": path_str, "full_hash": fh})
                result.files_errored += 1
                # Periodic commit so progress survives a crash
                if result.files_processed % _COMMIT_EVERY == 0:
                    ctx.conn.commit()
                    logger.info(
                        "[sentinel] checkpoint %d / %d",
                        result.files_processed,
                        len(pending),
                    )
                continue

            assert ah is not None
            upsert_archive(
                ctx.conn,
                {
                    "file_path": path_str,
                    "audio_hash": ah,
                    "full_hash": fh,
                    "status": "HASHED",
                },
            )
            ctx.log_event(
                "HASH_COMPUTED",
                file_path=path_str,
                new_value=ah[:16] + "…",
                stage=self.NAME,
            )
            result.files_changed += 1

            # Duplicate detection
            if ah in seen_hashes:
                first = seen_hashes[ah]
                if first != path_str:
                    group_id = f"dup_{ah[:12]}"
                    # Insert both sides (ON CONFLICT IGNORE for idempotency)
                    for fp in (first, path_str):
                        ctx.conn.execute(
                            """
                            INSERT OR IGNORE INTO duplicates
                                (group_id, file_path, duplicate_type, confidence, run_id)
                            VALUES (?, ?, 'EXACT', 1.0, ?)
                            """,
                            (group_id, fp, ctx.run_id),
                        )
                    ctx.log_event(
                        "DUPLICATE_FOUND",
                        file_path=path_str,
                        old_value=first,
                        stage=self.NAME,
                        note=f"EXACT group={group_id}",
                    )
                    result.notes.append(
                        f"DUPLICATE: {Path(path_str).name} == {Path(first).name}"
                    )
            else:
                seen_hashes[ah] = path_str

            # Periodic commit so progress survives a crash
            if result.files_processed % _COMMIT_EVERY == 0:
                ctx.conn.commit()
                logger.info(
                    "[sentinel] checkpoint %d / %d",
                    result.files_processed,
                    len(pending),
                )

        if result.files_errored > 0:
            result.success = False

        ctx.record_stage(result)
        return result
