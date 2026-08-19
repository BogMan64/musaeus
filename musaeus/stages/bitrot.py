#!/usr/bin/env python3
"""
MUSAEUS — Bit-Rot Check Stage (standalone, not wired into DEFAULT_PIPELINE)

Re-hashes every CATALOGUED file and compares against archive.full_hash
(already computed by SentinelStage at intake) to catch silent bit rot --
corruption that doesn't break playback or decoding, so nothing else in
the pipeline would ever notice it. Ported from ORPHEUS's
orpheus_integrity_check.py's --verify mode, per tonight's 222-script
ORPHEUS salvage audit.

Why this is genuinely different from the existing IntegrityStage
(despite that stage's own docstring listing orpheus_integrity_check.py
as an "ORPHEUS equivalent" -- checked directly, confirmed that's an
overclaim): IntegrityStage runs an ffmpeg decode-test ONCE per file
(gated on integrity_checked_at IS NULL, never re-checked again) --
it catches files that fail to *decode*. This stage re-hashes and
compares against a stored baseline, catching files whose bytes have
silently *changed* since they were last known-good, even if ffmpeg would
still decode them without complaint (e.g. a handful of flipped bits deep
in PCM data). Decode-validity and byte-identity are different
properties; this project already had a column for the second
(archive.full_hash, commented "for change detection") with nothing that
actually used it that way.

Design differences from the ORPHEUS original:
  - Reuses archive.full_hash as the baseline (already computed by
    Sentinel at intake) instead of a separate CSV hash database --
    ORPHEUS's --generate step is simply unnecessary here, the baseline
    already exists. It is NOT always kept current, though -- confirmed
    live against the real vault (2026-08-19): build_alac_library.py
    (Phase 2A) legitimately rewrites a file's bytes when baking LUFS but
    never refreshes full_hash afterward, so every one of the 1,385 files
    baked so far has a stale baseline. This stage excludes
    lufs_baked_at IS NOT NULL rows from its candidate set for exactly
    that reason (see _get_candidates) rather than reporting 1,385 false
    "corrupt" findings on its first real run. Tracked as a separate,
    real, not-yet-fixed gap -- either build_alac_library.py should
    refresh full_hash after a successful bake, or a one-time backfill is
    needed before this stage can meaningfully cover baked content.
  - DB-row-driven (archive table, status='CATALOGUED'), not a directory
    walk.
  - bitrot_checked_at is NOT a skip-if-set resumability gate, unlike
    every other nullable-timestamp column added tonight. Catching newly
    occurring corruption means re-verifying the same files on every run
    -- the column exists purely so a human can see "when was this file
    last confirmed intact," not to make the stage skip work.
  - Report-only, matching the original: a mismatch is logged and
    reported, archive.full_hash is never overwritten with the new
    (possibly corrupt) hash -- doing that would silently erase the
    ability to detect the same corruption again next run.
  - Dropped the original's --deep frame-level scan (FLAC sync-code /
    MP3 sync-byte / M4A atom scanning) -- CorruptStage already does a
    real ffprobe decode-test over the same files (a strictly more
    reliable check than hand-rolled frame-sync byte scanning), so
    porting a second, weaker version of the same idea isn't worth it.
  - Rows with no full_hash yet (never hashed by Sentinel) are skipped,
    not treated as new/errors -- there's nothing to compare against.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from ..context import RunContext, StageResult
from ..hasher import file_hash
from .base import BaseStage

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 25


class BitRotStage(BaseStage):
    """
    Re-hash every CATALOGUED file and compare against archive.full_hash
    to catch silent bit rot. Standalone -- not part of DEFAULT_PIPELINE.
    Report-only: never overwrites full_hash, never moves/deletes files.
    Use ctx.set("bitrot_limit", N) to cap how many rows a single run
    checks (full-library hashing is I/O heavy; useful for staged runs).
    """

    NAME = "bitrot"

    def validate(self, ctx: RunContext) -> None:
        """No external dependency to check -- pure Python hashing via
        the same helper Sentinel itself uses to compute full_hash."""

    def _get_candidates(self, ctx: RunContext) -> list[dict]:
        # Excludes lufs_baked_at IS NOT NULL rows -- discovered live against
        # the real vault (2026-08-19): build_alac_library.py legitimately
        # rewrites a file's bytes when baking LUFS, but never refreshes
        # full_hash afterward. Without this exclusion, every one of the
        # 1,385 files Phase 2A has baked so far would report as a false
        # "bit rot" mismatch on this stage's very first real run -- the
        # hash is stale relative to an intentional change, not evidence of
        # corruption. Tracked as a separate, real gap (not fixed here):
        # either build_alac_library.py should refresh full_hash after a
        # successful bake, or a one-time backfill is needed before this
        # stage can meaningfully cover baked content.
        rows = ctx.conn.execute(
            """
            SELECT id, file_path, full_hash FROM archive
             WHERE status = 'CATALOGUED' AND full_hash IS NOT NULL AND full_hash != ''
               AND lufs_baked_at IS NULL
             ORDER BY file_path
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def _run_checks(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        candidates = self._get_candidates(ctx)

        limit = ctx.get("bitrot_limit", 0)
        if limit:
            candidates = candidates[:limit]

        result.notes.append(f"files to check: {len(candidates)}")
        if not candidates:
            result.notes.append("nothing to do — no hashed CATALOGUED rows found")
            ctx.record_stage(result)
            return result

        if dry_run:
            result.files_processed = len(candidates)
            result.notes.append("[DRY RUN] no files will be hashed, no DB changes")
            ctx.record_stage(result)
            return result

        now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        corrupt: list[tuple[str, str, str]] = []  # (file_path, stored, current)
        ok_count = 0
        missing = 0

        for i, row in enumerate(candidates, 1):
            result.files_processed += 1
            path = Path(row["file_path"])

            if not path.exists():
                # GhostStage's job to record this -- just skip counting it
                # here so a genuinely missing file doesn't masquerade as
                # a bit-rot finding.
                result.files_skipped += 1
                missing += 1
                continue

            try:
                current_hash = file_hash(path)
            except OSError as exc:
                result.files_errored += 1
                result.errors.append(f"{path.name}: could not read file: {exc}")
                continue

            if current_hash != row["full_hash"]:
                corrupt.append((row["file_path"], row["full_hash"], current_hash))
                ctx.conn.execute(
                    "UPDATE archive SET bitrot_ok = 0, bitrot_checked_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                ctx.log_event(
                    "BITROT_DETECTED",
                    file_path=row["file_path"],
                    old_value=row["full_hash"],
                    new_value=current_hash,
                    stage=self.NAME,
                    note="file bytes changed since last known-good hash",
                )
                logger.warning("[bitrot] MISMATCH: %s", path.name)
            else:
                ok_count += 1
                ctx.conn.execute(
                    "UPDATE archive SET bitrot_ok = 1, bitrot_checked_at = ? WHERE id = ?",
                    (now, row["id"]),
                )

            if i % _COMMIT_EVERY == 0:
                ctx.conn.commit()
                logger.info("bitrot: checkpoint %d/%d", i, len(candidates))

        ctx.conn.commit()

        result.notes.append(f"ok: {ok_count}")
        result.notes.append(f"corrupt (hash mismatch): {len(corrupt)}")
        result.notes.append(f"missing from disk: {missing}")
        if corrupt:
            result.success = False
            for fp, stored, current in corrupt[:20]:
                result.notes.append(f"  MISMATCH {fp}  (stored {stored[:12]}… now {current[:12]}…)")
            if len(corrupt) > 20:
                result.notes.append(f"  ... and {len(corrupt) - 20} more")

        ctx.record_stage(result)
        return result

    def run(self, ctx: RunContext) -> StageResult:
        return self._run_checks(ctx, dry_run=False)

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._run_checks(ctx, dry_run=True)
