#!/usr/bin/env python3
"""
MUSAEUS — CrossDupe Stage (Act 2)

Checks incoming files against the PERSISTENT cross-batch hash index
(config.hash_index_path) — the audio hashes of everything already
finalized into ALAC-Library in a PRIOR batch. This is the only way to
catch "this exact song is already in the library" once musaeus.db has
been wiped at the end of a completed batch (Grey's explicit design
decision: no persistent DB across batches — see config.py docstring).

Ordering: this needs audio_hash, which only Sentinel produces, so it
cannot literally run BEFORE Sentinel. It runs immediately after Sentinel
instead — as early as a hash-based check can possibly run — so a file
that's already archived gets flagged before any further work
(Scholar's ffprobe read, Canonicalize's ffmpeg conversion, Forge's
loudness measurement, Tagger's writes) is wasted on it. This is
distinct from Sentinel's own duplicate detection, which only compares
files WITHIN the current batch against each other — CrossDupe compares
against files from ALL PRIOR batches.

What it does:
  - For every archive row with audio_hash set and no existing
    CROSS_BATCH duplicates-table entry yet (idempotent — safe to re-run
    within the same batch)
  - Looks up that hash in the persistent hash index
  - If found, stages it in the `duplicates` table with
    duplicate_type='CROSS_BATCH', logs a CROSS_BATCH_DUPLICATE_FOUND
    event, and leaves a note pointing at the exact ALAC-Library path(s)
    it matches
  - Does NOT move, quarantine, or delete anything, and does NOT block
    the file from continuing through the rest of the pipeline — per
    Grey's decided dedupe shape (exact/near duplicates are never
    auto-resolved), this is a flag for human review via `musaeus
    dedupe`, not an automatic action
  - dry_run() reports matches without writing to the duplicates table

If config.hash_index_path doesn't exist yet (e.g. the very first batch
ever run, before anything has been finalized), this is a normal no-op —
nothing can match an empty index.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..context import RunContext, StageResult
from ..db import lookup_finalized_hash, open_hash_index
from .base import BaseStage

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 100


def _get_candidates(conn) -> list[dict]:  # type: ignore[type-arg]
    """
    Archive rows with a hash to check, that haven't already been flagged
    as a CROSS_BATCH duplicate this batch (idempotent re-run guard).
    """
    rows = conn.execute(
        """
        SELECT a.file_path, a.audio_hash
          FROM archive a
         WHERE a.audio_hash IS NOT NULL
           AND NOT EXISTS (
                 SELECT 1 FROM duplicates d
                  WHERE d.file_path = a.file_path
                    AND d.duplicate_type = 'CROSS_BATCH'
               )
         ORDER BY a.file_path
        """
    ).fetchall()
    return [dict(r) for r in rows]


class CrossDupeStage(BaseStage):
    """
    CrossDupe — flag incoming files that already exist in ALAC-Library
    from a prior batch, using the persistent cross-batch hash index.
    """

    NAME = "cross-dupe"

    def validate(self, ctx: RunContext) -> None:
        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE audio_hash IS NOT NULL"
        ).fetchone()[0]
        logger.info("[cross-dupe] %d hashed file(s) to check against ALAC-Library", count)

    # ── Shared logic ──────────────────────────────────────────────────────────

    def _check(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        candidates = _get_candidates(ctx.conn)
        result.files_processed = len(candidates)

        if not candidates:
            result.notes.append("nothing to check — no hashed files pending")
            ctx.record_stage(result)
            return result

        if not ctx.config.hash_index_path.exists():
            result.notes.append(
                "no cross-batch hash index yet (first batch, or nothing "
                "finalized so far) — nothing to match against"
            )
            ctx.record_stage(result)
            return result

        hash_conn = open_hash_index(ctx.config.hash_index_path)
        matches = 0
        stale = 0

        try:
            for i, row in enumerate(candidates, 1):
                path_str = row["file_path"]
                ah = row["audio_hash"]

                existing = lookup_finalized_hash(hash_conn, ah)
                if not existing:
                    continue

                # The ledger records that a file was once FINALIZED, not that
                # a copy is held right now: it is append-only (deliberately --
                # it must survive DB resets) and nothing prunes an entry when
                # the file it names is later moved. Acting on an unverified
                # hit is what caused the 2026-08-17/18 cascade: once a file
                # had been relocated into DUPES_MOVED_FOR_REVIEW, its stale
                # entry outlived it and the next pass quarantined it as a
                # duplicate of ITSELF. 10,597 tracks ended up as the only
                # copy of their own audio, filed as duplicates of nothing.
                #
                # So confirm the twin is really there before believing it,
                # and never count the candidate's own path as its twin.
                # See scope doc section 4.17.
                indexed_twins = [r for r in existing if r["file_path"] != path_str]
                if not indexed_twins:
                    # The only entry is the candidate's own path -- the exact
                    # cascade shape. Not a stale ledger, just nothing to
                    # compare against, so it must not be reported as one.
                    continue

                live_paths = [
                    r["file_path"] for r in indexed_twins if Path(r["file_path"]).exists()
                ]
                if not live_paths:
                    stale += 1
                    logger.debug(
                        "stale hash-index hit for %s: indexed copies gone (%s)",
                        path_str,
                        "; ".join(r["file_path"] for r in indexed_twins[:2]),
                    )
                    continue

                matches += 1
                matched_paths = live_paths
                note = "already in ALAC-Library: " + "; ".join(matched_paths[:3])
                if len(matched_paths) > 3:
                    note += f" (+{len(matched_paths) - 3} more)"

                result.files_changed += 1
                result.notes.append(f"CROSS-BATCH DUPLICATE: {Path(path_str).name} — {note}")

                if not dry_run:
                    group_id = f"crossdupe_{ah[:12]}"
                    ctx.conn.execute(
                        """
                        INSERT OR IGNORE INTO duplicates
                            (group_id, file_path, duplicate_type, confidence, run_id)
                        VALUES (?, ?, 'CROSS_BATCH', 1.0, ?)
                        """,
                        (group_id, path_str, ctx.run_id),
                    )
                    ctx.log_event(
                        "CROSS_BATCH_DUPLICATE_FOUND",
                        file_path=path_str,
                        stage=self.NAME,
                        note=note,
                    )

                if not dry_run and i % _COMMIT_EVERY == 0:
                    ctx.conn.commit()
                    logger.info("cross-dupe: checkpoint %d/%d", i, len(candidates))
        finally:
            hash_conn.close()

        if not dry_run:
            ctx.conn.commit()

        if stale:
            # Surfaced, not silent: a large stale count means the ledger has
            # drifted from the filesystem and wants pruning.
            result.notes.append(
                f"ignored {stale} stale hash-index hit(s) — indexed file no longer on disk"
            )

        if matches == 0:
            result.notes.append("no cross-batch duplicates found")
        else:
            prefix = "Would flag" if dry_run else "Flagged"
            result.notes.append(f"{prefix} {matches} cross-batch duplicate(s) for review")
            result.notes.append("Review with: musaeus dedupe")

        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._check(ctx, dry_run=True)

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """A duplicate this stage found must actually be staged.

        The event says "found"; the row in `duplicates` is what the
        resolver later acts on. An event with no staged row means the
        finding is reported to a human and invisible to the pipeline --
        the dupe is never resolved and nothing ever says so again.
        """
        rows = ctx.conn.execute(
            "SELECT file_path FROM events WHERE run_id = ? "
            " AND event_type = 'CROSS_BATCH_DUPLICATE_FOUND' ORDER BY id DESC LIMIT 10",
            (ctx.run_id,),
        ).fetchall()
        if not rows:
            return []
        unstaged = [
            Path(r["file_path"]).name
            for r in rows
            if not ctx.conn.execute(
                "SELECT 1 FROM duplicates WHERE file_path = ? "
                " AND duplicate_type = 'CROSS_BATCH' LIMIT 1",
                (r["file_path"],)).fetchone()
        ]
        if not unstaged:
            return []
        return [
            f"{len(unstaged)} of {len(rows)} cross-batch duplicate(s) were "
            f"reported but never staged: {', '.join(unstaged[:3])}"
        ]

    def run(self, ctx: RunContext) -> StageResult:
        return self._check(ctx, dry_run=False)
