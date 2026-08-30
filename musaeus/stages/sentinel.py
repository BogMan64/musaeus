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
        SELECT file_path, audio_hash, status FROM archive
        WHERE status = 'PENDING'
           OR audio_hash IS NULL
        ORDER BY file_path
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _hash_group_for(conn, audio_hash_val: str) -> list[str]:
    """Return all file_paths that share the given audio_hash."""
    rows = conn.execute(
        "SELECT file_path FROM archive "
        " WHERE audio_hash = ? "
        "   AND status NOT IN ('GHOST', 'QUARANTINED', 'DELETED')",
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
        count = ctx.conn.execute("SELECT COUNT(*) FROM archive WHERE status='PENDING'").fetchone()[
            0
        ]
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
        # A row whose file is gone is not something a new file can duplicate.
        #
        # This preloaded EVERY row with an audio_hash, so a GHOST or
        # QUARANTINED phantom -- a row for a file already moved away or
        # deleted -- was a valid match target, and an incoming file was
        # quarantined as a duplicate of something that no longer exists.
        # Same family as the dupe cascade cross_dupe.py was hardened against;
        # it learned to verify liveness, Sentinel had not.
        #
        # Filtered on status AND checked on disk: the status is the cheap
        # answer and the filesystem is the true one, and they disagree
        # exactly when a row is stale, which is the case that matters.
        existing = ctx.conn.execute(
            "SELECT file_path, audio_hash FROM archive "
            " WHERE audio_hash IS NOT NULL "
            "   AND status NOT IN ('GHOST', 'QUARANTINED', 'DELETED')"
        ).fetchall()
        for row in existing:
            h = row["audio_hash"]
            if h in seen_hashes:
                continue
            if not Path(row["file_path"]).exists():
                continue
            seen_hashes[h] = row["file_path"]

        _COMMIT_EVERY = 50  # commit progress incrementally

        for row in pending:
            path_str = row["file_path"]
            path = Path(path_str)
            result.files_processed += 1

            if not path.exists():
                # Mark, do not DELETE.
                #
                # This used to DELETE the archive row, which contradicted two
                # things at once: GhostStage exists precisely to mark missing
                # files as status='GHOST' and log GHOST_FOUND, and the event
                # log is meant to be the source of truth -- a deleted row
                # leaves nothing to reconcile when the file comes back. A
                # temporarily-unmounted drive silently erased its own history.
                #
                # Same transition GhostStage performs, so the two agree.
                logger.warning("Missing file — marking GHOST: %s", path_str)
                ctx.conn.execute(
                    "UPDATE archive SET status='GHOST', last_seen=datetime('now') "
                    "WHERE file_path=?",
                    (path_str,),
                )
                ctx.log_event(
                    "GHOST_FOUND",
                    file_path=path_str,
                    old_value=row.get("status"),
                    new_value="GHOST",
                    stage=self.NAME,
                    note="missing on disk during sentinel scan",
                )
                result.files_errored += 1
                result.errors.append(f"Missing file, marked GHOST: {path_str}")
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
            # Hashing advances a NEW file to HASHED. It must not DEMOTE a row
            # that is already further along.
            #
            # This wrote status='HASHED' unconditionally, so re-hashing a
            # finalized library file knocked it out of CATALOGUED and made it
            # invisible to Tagger, Organize and Audit -- all of which select
            # status='CATALOGUED' -- while making it eligible for the whole
            # downstream chain again. Measured 2026-08-30: a re-hash pass over
            # the library demoted 1,100 finalized rows before it was stopped,
            # and would have taken all 9,556.
            #
            # A row is re-hashed for its hashes, not for its position.
            fields = {"file_path": path_str, "audio_hash": ah, "full_hash": fh}
            if (row.get("status") or "PENDING") == "PENDING":
                fields["status"] = "HASHED"
            upsert_archive(ctx.conn, fields)
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
                    result.notes.append(f"DUPLICATE: {Path(path_str).name} == {Path(first).name}")
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
