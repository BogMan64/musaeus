#!/usr/bin/env python3
"""
MUSAEUS — Bit-Rot Check Stage (standalone, not wired into DEFAULT_PIPELINE)

Detects silent corruption in ALAC_Archive -- the pristine, permanent
tier -- by comparing each file's current SHA-256 against a baseline
recorded in archive_tier_hashes. Ported from ORPHEUS's
orpheus_integrity_check.py (both --generate and --verify modes), per
tonight's 222-script ORPHEUS salvage audit.

Second design, same session (2026-08-19): the first version compared
against archive.full_hash instead. Live-vault testing found that stale
for nearly every finalized file -- full_hash is computed by Sentinel
early in the pipeline, well before Canonicalize/Forge/Tagger legitimately
rewrite file bytes, so it was never actually valid post-Finalize for
this purpose (it's fine for what it was actually built for: Sentinel's
own retag-vs-audio-change detection). Corrected per Grey's own framing:
trust ALAC_Archive's *current* state as the baseline (it's the pristine,
permanent tier -- nothing modifies it after migration) and establish a
*fresh* hash from that state, rather than reconciling against an
already-mismatched value.

Why this checks ALAC_Archive specifically, not ALAC-Library: the archive
tier is the one place in this project that's supposed to stay byte-
identical forever once a file lands there. ALAC-Library gets
legitimately rewritten by Phase 2A LUFS baking, so "did this file
change" isn't a meaningful question to ask about it -- of course it did,
on purpose. If ALAC-Library content is ever suspected corrupted, the
correct move is re-baking from the archive origin, not restoring a
byte-for-byte backup.

Deliberately NOT tied to archive.id / archive.file_path: ALAC_Archive is
itself deliberately not DB-row-tracked (build_alac_library.py's own
docstring explains why -- avoiding a second path column that could
drift out of sync with real filesystem state, since archive.file_path
already gets repointed at the baked ALAC-Library copy once a row is
baked). Tying bit-rot verification to that same row/path would inherit
the same fragility; confirmed the hard way while investigating this
tonight -- reconstructing "what was in ALAC_Archive for this row" from
current archive.file_path or even the LUFS_BAKE event log's old_value
was unreliable once other stages (dupe-resolver, etc.) had moved things
again since. A plain directory scan of ALAC_Archive, keyed by path in
its own dedicated table, sidesteps all of that.

Two modes:
  --rebaseline: scan ALAC_Archive, record each file's current SHA-256
    into archive_tier_hashes (INSERT OR REPLACE). Deliberate and
    explicit only -- never automatic, since silently re-baselining on
    every run would absorb real corruption into "the new normal"
    instead of ever catching it. Establishes ORPHEUS's --generate.
  (default): scan ALAC_Archive, compare each file's current SHA-256
    against its stored baseline. Mismatch = flagged corrupt. A file with
    no baseline entry yet is reported as new (needs a --rebaseline
    pass), not treated as corrupt. A baseline entry with no matching
    file on disk is reported as missing, separately. Establishes
    ORPHEUS's --verify (its --deep frame-level scan is dropped for the
    same reason the first design dropped it: CorruptStage's real
    ffprobe decode-test already covers that ground more reliably than
    hand-rolled frame-sync byte scanning).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import AUDIO_EXTENSIONS
from ..context import RunContext, StageResult, elision
from ..hasher import file_hash
from .base import BaseStage

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 25


def _scan_archive_files(alac_archive: Path) -> list[Path]:
    if not alac_archive.exists():
        return []
    return sorted(
        p for p in alac_archive.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )


class BitRotStage(BaseStage):
    """
    Detect silent corruption in ALAC_Archive by comparing current
    SHA-256 hashes against a baseline in archive_tier_hashes. Standalone
    -- not part of DEFAULT_PIPELINE, and does not touch archive.* rows
    at all (directory-scan based, keyed by path).

    Use ctx.set("bitrot_rebaseline", True) to (re)establish the baseline
    from the archive's current state instead of verifying against it.
    Use ctx.set("bitrot_limit", N) to cap how many files a single run
    processes (full-archive hashing is I/O heavy).
    """

    NAME = "bitrot"

    def validate(self, ctx: RunContext) -> None:
        """No external dependency to check -- pure Python hashing via
        the same helper Sentinel itself uses to compute full_hash."""

    def _rebaseline(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        files = _scan_archive_files(ctx.config.alac_archive)

        limit = ctx.get("bitrot_limit", 0)
        if limit:
            files = files[:limit]

        result.notes.append(f"files to baseline: {len(files)}")
        if not files:
            result.notes.append("nothing to do — ALAC_Archive is empty or missing")
            ctx.record_stage(result)
            return result

        if dry_run:
            result.files_processed = len(files)
            result.notes.append("[DRY RUN] no hashing, no baseline written")
            ctx.record_stage(result)
            return result

        for i, path in enumerate(files, 1):
            result.files_processed += 1
            try:
                h = file_hash(path)
            except OSError as exc:
                result.files_errored += 1
                result.errors.append(f"{path.name}: could not read file: {exc}")
                continue

            ctx.conn.execute(
                """
                INSERT INTO archive_tier_hashes (path, sha256, size_bytes, baselined_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(path) DO UPDATE SET
                    sha256       = excluded.sha256,
                    size_bytes   = excluded.size_bytes,
                    baselined_at = excluded.baselined_at
                """,
                (str(path), h, path.stat().st_size),
            )
            result.files_changed += 1

            if i % _COMMIT_EVERY == 0:
                ctx.conn.commit()
                logger.info("bitrot: baseline checkpoint %d/%d", i, len(files))

        ctx.conn.commit()
        result.notes.append(f"baselined: {result.files_changed}")
        if result.files_errored:
            result.success = False

        ctx.record_stage(result)
        return result

    def _verify(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        alac_archive = ctx.config.alac_archive
        files = _scan_archive_files(alac_archive)

        limit = ctx.get("bitrot_limit", 0)
        if limit:
            files = files[:limit]

        result.notes.append(f"files to verify: {len(files)}")

        baseline = {
            row["path"]: row["sha256"]
            for row in ctx.conn.execute("SELECT path, sha256 FROM archive_tier_hashes").fetchall()
        }

        # A baselined file can be missing on disk even when the current
        # scan finds nothing at all (e.g. every baselined file was
        # removed) -- that's still worth reporting, not "nothing to do".
        if not files and not baseline:
            result.notes.append("nothing to do — ALAC_Archive is empty or missing")
            ctx.record_stage(result)
            return result

        if dry_run:
            result.files_processed = len(files)
            result.notes.append("[DRY RUN] no hashing, no DB changes")
            ctx.record_stage(result)
            return result

        ok_count = 0
        new_files = 0
        corrupt: list[tuple[str, str, str]] = []

        for i, path in enumerate(files, 1):
            result.files_processed += 1
            path_str = str(path)

            if path_str not in baseline:
                new_files += 1
                result.files_skipped += 1
                continue

            try:
                current_hash = file_hash(path)
            except OSError as exc:
                result.files_errored += 1
                result.errors.append(f"{path.name}: could not read file: {exc}")
                continue

            if current_hash != baseline[path_str]:
                corrupt.append((path_str, baseline[path_str], current_hash))
                ctx.log_event(
                    "BITROT_DETECTED",
                    file_path=path_str,
                    old_value=baseline[path_str],
                    new_value=current_hash,
                    stage=self.NAME,
                    note="ALAC_Archive file bytes changed since baseline",
                )
                logger.warning("[bitrot] MISMATCH: %s", path.name)
            else:
                ok_count += 1

            if i % _COMMIT_EVERY == 0:
                logger.info("bitrot: verify checkpoint %d/%d", i, len(files))

        missing = [p for p in baseline if not Path(p).exists()]

        result.notes.append(f"ok: {ok_count}")
        result.notes.append(f"corrupt (hash mismatch): {len(corrupt)}")
        result.notes.append(f"new (no baseline yet — run --rebaseline): {new_files}")
        result.notes.append(f"missing from disk (was baselined, gone now): {len(missing)}")
        if corrupt:
            result.success = False
            for fp, stored, current in corrupt[:20]:
                result.notes.append(
                    f"  MISMATCH {fp}  (baseline {stored[:12]}… now {current[:12]}…)"
                )
            if len(corrupt) > 20:
                result.notes.append(f"  {elision(len(corrupt) - 20)}")

        ctx.record_stage(result)
        return result

    def run(self, ctx: RunContext) -> StageResult:
        if ctx.get("bitrot_rebaseline", False):
            return self._rebaseline(ctx, dry_run=False)
        return self._verify(ctx, dry_run=False)

    def dry_run(self, ctx: RunContext) -> StageResult:
        if ctx.get("bitrot_rebaseline", False):
            return self._rebaseline(ctx, dry_run=True)
        return self._verify(ctx, dry_run=True)
