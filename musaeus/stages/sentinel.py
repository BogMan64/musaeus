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

#: SHA-256 of a zero-byte stream.
#:
#: audio_hash() returns h.hexdigest() whenever ffmpeg exits 0 -- including
#: when it decoded nothing at all. A file whose audio stream yields no PCM
#: therefore gets a perfectly well-formed 64-char hash that describes no
#: audio, and every such file gets the SAME one.
#:
#: That is the worst available outcome for this stage: identical hashes are
#: how Sentinel defines an EXACT duplicate, so a batch of these files is
#: filed as one giant duplicate group and dupe-resolver keeps one and
#: archives the rest. Nothing in the run reports an error -- the hash was
#: computed, the row was updated, the count went up.
_EMPTY_STREAM_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


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

    # ── Verify ────────────────────────────────────────────────────────────────

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """Confirm the hashes this run computed describe the files they name.

        Sentinel is the stage whose silent failure costs the most. Every
        dedup decision downstream -- EXACT here, NEAR in neardupe, the
        connected components dupe_resolver archives files on -- is a
        statement about audio_hash. A hash that is merely PRESENT is enough
        to satisfy all of them, so a wrong hash is not caught anywhere else
        in the pipeline; it is acted upon.

        Three checks, cheapest first:

        1. The empty-stream digest. ffmpeg exiting 0 having decoded nothing
           produces a well-formed hash of no audio, identical for every such
           file -- which this stage reads as an EXACT duplicate group. This
           runs over the whole batch, not a sample: one such row is a
           reportable fault, and finding it in a sample is luck.

        2. The row actually carries the hash and left PENDING. A row logged
           as HASH_COMPUTED whose audio_hash is still NULL means the upsert
           did not land; one still at PENDING is re-hashed every run for
           ever, which is how a stage can burn hours and appear to work.

        3. Re-derive the hash from the file for a few rows, and compare.
           This is the only check that asks the artifact rather than the
           bookkeeping. It costs a full audio decode per sampled file, so
           the sample is deliberately tiny -- the point is catching a stage
           that hashed the wrong thing, not re-hashing the library.
        """
        # Aggregated over the archive rows themselves, selected by a
        # subquery rather than a JOIN: joining events multiplies a row by
        # its event count, so every SUM here would over-count a file that
        # was hashed twice while COUNT(DISTINCT) did not. The ratios are
        # the whole message of these three problems.
        hashed = ctx.conn.execute(
            """
            SELECT COUNT(*) AS n,
                   SUM(audio_hash IS NULL OR audio_hash = '') AS no_hash,
                   SUM(status = 'PENDING') AS still_pending,
                   SUM(audio_hash = ?) AS empty_stream
              FROM archive
             WHERE file_path IN (
                 SELECT file_path FROM events
                  WHERE stage = ? AND event_type = 'HASH_COMPUTED' AND run_id = ?
             )
            """,
            (_EMPTY_STREAM_SHA256, self.NAME, ctx.run_id),
        ).fetchone()

        n = hashed["n"] or 0
        if n == 0:
            # The stage counted changes but logged no HASH_COMPUTED events.
            # Reporting [] here would be the hollow "verified" this hook
            # exists to prevent: there is nothing to have looked at.
            return [
                f"stage reported {result.files_changed} hashed file(s) but the "
                f"event log has no HASH_COMPUTED for this run"
            ]

        problems: list[str] = []

        if hashed["empty_stream"]:
            example = ctx.conn.execute(
                "SELECT file_path FROM archive WHERE audio_hash = ? LIMIT 1",
                (_EMPTY_STREAM_SHA256,),
            ).fetchone()
            problems.append(
                f"{hashed['empty_stream']} of {n} file(s) carry the empty-stream "
                f"digest — decoded to no audio, and they will read as one EXACT "
                f"duplicate group, e.g. {Path(example['file_path']).name}"
            )
        if hashed["no_hash"]:
            problems.append(
                f"{hashed['no_hash']} of {n} file(s) logged HASH_COMPUTED but have "
                f"no audio_hash stored — the update did not land"
            )
        if hashed["still_pending"]:
            problems.append(
                f"{hashed['still_pending']} of {n} hashed file(s) are still PENDING — "
                f"they will be re-hashed on every run"
            )

        sample = ctx.conn.execute(
            """
            SELECT DISTINCT a.file_path, a.audio_hash, a.full_hash
              FROM archive a
              JOIN events e ON e.file_path = a.file_path
             WHERE e.stage = ? AND e.event_type = 'HASH_COMPUTED'
               AND e.run_id = ? AND a.audio_hash IS NOT NULL
             ORDER BY e.id DESC LIMIT 3
            """,
            (self.NAME, ctx.run_id),
        ).fetchall()

        for row in sample:
            path = Path(row["file_path"])
            if not path.exists():
                problems.append(f"hashed file is not on disk: {path.name}")
                continue
            # A hash that equals the file hash was the documented timeout
            # fallback inside audio_hash(), not a PCM hash, and re-deriving
            # it may legitimately take the other branch this time. Reporting
            # that as a mismatch would be crying wolf about working code.
            if row["full_hash"] and row["audio_hash"] == row["full_hash"]:
                continue
            again, err = audio_hash_safe(path)
            if err:
                problems.append(f"{path.name}: stored a hash but cannot be re-hashed: {err}")
            elif again != row["audio_hash"]:
                problems.append(
                    f"{path.name}: re-hashing gives {again[:12]}… but "
                    f"{row['audio_hash'][:12]}… was stored — the hash does not "
                    f"describe this file"
                )

        return problems

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
            # PENDING advances. A GHOST whose file is back RECOVERS -- it is
            # here because the file exists again, and GhostStage only ever
            # SETS ghost, so this is the one automatic un-ghost path; without
            # it a remounted drive left every row invisible to Scholar,
            # Canonicalize, Tagger, Organize and Audit for ever.
            # Anything further along keeps its position (see above).
            if (row.get("status") or "PENDING") in ("PENDING", "GHOST"):
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
