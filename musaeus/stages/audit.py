#!/usr/bin/env python3
"""
MUSAEUS — Audit Stage (Act 3)

The final gate before a batch's musaeus.db can be snapshotted and wiped
(task 8). Report-only: never mutates the archive table, never moves or
deletes a file. Its only job is to answer one question honestly:
"does ALAC-Library on disk actually contain what the DB thinks it
finalized?" -- the same physical-presence-over-DB-trust principle
Finalize is built on, applied one more time as an explicit checkpoint
rather than assumed.

Two directions are checked, matching the gap identified in ORPHEUS's
own orpheus_ghost_sweep.py (which only checks one direction -- confirmed
via investigation, not assumed):

  1. DB-says-finalized -> does the file actually exist on disk, at the
     path the DB claims, inside ALAC-Library? (This is GhostStage's
     check, but scoped specifically to rows this batch finalized, since
     that's the population Finalize just claimed to have moved.)

  2. Disk-has-a-file-in-ALAC-Library -> does the archive table actually
     have a matching finalized row for it? A file could land in
     ALAC-Library outside Finalize entirely (a manual copy, a partially
     applied fix, a stray leftover from testing) and nothing would
     otherwise notice. This is the direction ORPHEUS's ghost sweep does
     NOT do.

  3. A third, MUSAEUS-specific check not present in ORPHEUS at all:
     every finalized row's audio_hash must actually be present in the
     persistent cross-batch hash index. Finalize writes this in the
     same run, but if that write were ever silently lost (e.g. a crash
     between the archive UPDATE and the hash_conn.commit()), CrossDupe
     in a future batch would have no way to know this file already
     exists -- Audit is the checkpoint that would catch that gap before
     the DB carrying the only other record of it is wiped.

Directories deliberately excluded from the disk-side scan:
  _history/    -- DB snapshots + the hash index database itself, not
                  audio content
  DUPES_MOVED_FOR_REVIEW/ -- resolved-duplicate holding area; files here
                  are legitimately not represented by a normal
                  "finalized" archive row (that mechanism is separate,
                  still being designed -- see project notes)

Exit contract: result.success is False if ANY mismatch is found in any
of the three checks above. The caller (the eventual db-snapshot-and-wipe
command, task 8) is expected to refuse to wipe when this stage reports
failure, and leave the DB alone so the mismatch can still be
investigated.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import AUDIO_EXTENSIONS
from ..context import RunContext, StageResult
from ..db import open_hash_index
from .base import BaseStage

logger = logging.getLogger(__name__)

_EXCLUDED_SUBDIRS = frozenset({"_history", "DUPES_MOVED_FOR_REVIEW", "TRIBUTE_REMOVED_FOR_REVIEW"})


def _scan_alac_library_files(alac_library: Path) -> set[Path]:
    """Every real audio file physically present in ALAC-Library, excluding
    the special-purpose subdirectories that aren't normal archive content."""
    if not alac_library.exists():
        return set()
    found: set[Path] = set()
    for p in alac_library.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        try:
            rel_parts = p.relative_to(alac_library).parts
        except ValueError:
            continue
        if rel_parts and rel_parts[0] in _EXCLUDED_SUBDIRS:
            continue
        found.add(p.resolve())
    return found


def _is_within(path: Path, root: Path) -> bool:
    """True when *path* is *root* or lives under it. Both must be resolved."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class AuditStage(BaseStage):
    """
    Audit — physical-presence verification. Report-only, gates the
    DB-snapshot-and-wipe step. Never mutates anything.
    """

    @classmethod
    def plan_candidates(cls, conn, cfg) -> tuple[int, str]:
        """Rows this stage would act on. Read-only; see planner.py."""
        n = conn.execute("SELECT COUNT(*) FROM archive WHERE finalized_at IS NOT NULL").fetchone()[
            0
        ]
        return int(n), "finalized rows to verify on disk"

    NAME = "audit"

    def validate(self, ctx: RunContext) -> None:
        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE finalized_at IS NOT NULL"
        ).fetchone()[0]
        logger.info("[audit] %d finalized row(s) to verify", count)

    def _run_checks(self, ctx: RunContext) -> tuple[list[str], list[str]]:
        """Returns (ok_notes, problem_notes)."""
        ok: list[str] = []
        problems: list[str] = []

        finalized_rows = ctx.conn.execute(
            "SELECT id, file_path, audio_hash FROM archive WHERE finalized_at IS NOT NULL"
        ).fetchall()

        # ── Check 1: DB says finalized -> file must exist, under a FINAL root
        #
        # There are two final homes, not one. The 2026-08-31 vocabulary made
        # ALAC_Archive the MASTERS tier -- "Masters are never physically
        # altered after finalize" -- while ALAC-Library holds the current
        # batch. Checking only ALAC-Library reported every master as a
        # problem: 10,423 of this run's 10,428 audit failures were finalized
        # masters sitting exactly where the design says they belong, and the
        # 5 real ones were buried under them.
        #
        # OrganizeStage is right to refuse those files (organize.py's roots
        # deliberately exclude the archive, so masters are never reshuffled).
        # It was this expectation that was stale, not that refusal. A gate
        # that fails 10,423 times for correct state is the crying-wolf half
        # of SOP 4.27, and it blocks the DB-wipe workflow it exists to guard.
        final_roots = [
            r.resolve()
            for r in (ctx.alac_library, ctx.config.alac_archive)
            if r.exists()
        ]
        db_side_paths: set[Path] = set()

        for row in finalized_rows:
            db_path = Path(row["file_path"])
            db_side_paths.add(db_path.resolve() if db_path.exists() else db_path)

            if not db_path.exists():
                problems.append(f"DB says finalized but file missing on disk: {row['file_path']}")
                continue

            if final_roots:
                resolved = db_path.resolve()
                if not any(_is_within(resolved, root) for root in final_roots):
                    problems.append(
                        f"DB says finalized but file is under no final root "
                        f"(not ALAC-Library, not ALAC_Archive): {row['file_path']}"
                    )

        if finalized_rows and not any(
            "missing on disk" in p or "no final root" in p for p in problems
        ):
            ok.append(f"all {len(finalized_rows)} finalized row(s) verified present on disk")

        # ── Check 2: disk has a file in ALAC-Library -> DB must know about it
        disk_files = _scan_alac_library_files(ctx.alac_library)
        orphans = disk_files - db_side_paths
        for orphan in sorted(orphans):
            problems.append(
                f"file present in ALAC-Library with no matching finalized row: {orphan}"
            )

        if disk_files and not orphans:
            ok.append(
                f"all {len(disk_files)} file(s) in ALAC-Library have a matching finalized row"
            )

        # ── Check 3: every finalized row's hash must be in the persistent index
        #
        # Matches on audio_hash alone, not (audio_hash, file_path) -- mirroring
        # db.lookup_finalized_hash(), the actual production cross-batch dedup
        # lookup, which only ever keys on audio_hash. finalized_hashes.file_path
        # is documented (db.py) as "final ALAC-Library path at time of
        # finalize" -- an immutable historical snapshot, not something kept in
        # sync with archive.file_path afterward. A row finalized once and later
        # moved by DupeResolverStage (into DUPES_MOVED_FOR_REVIEW, updating
        # archive.file_path) is expected to no longer match its finalize-time
        # snapshot; audio_hash is the stable identity across any such move
        # (dupe_resolver.py's own stated design), so that's what this check
        # must key on too.
        if finalized_rows:
            hashed_rows = [r for r in finalized_rows if r["audio_hash"]]
            if hashed_rows:
                if ctx.config.hash_index_path.exists():
                    hash_conn = open_hash_index(ctx.config.hash_index_path)
                    try:
                        missing_from_index = []
                        for row in hashed_rows:
                            found = hash_conn.execute(
                                "SELECT 1 FROM finalized_hashes WHERE audio_hash = ?",
                                (row["audio_hash"],),
                            ).fetchone()
                            if not found:
                                missing_from_index.append(row["file_path"])
                    finally:
                        hash_conn.close()

                    for fp in missing_from_index:
                        problems.append(
                            f"finalized row's hash missing from persistent cross-batch index: {fp}"
                        )
                    if not missing_from_index:
                        ok.append(
                            f"all {len(hashed_rows)} finalized row(s) confirmed in the "
                            f"persistent cross-batch hash index"
                        )
                else:
                    problems.append(
                        f"{len(hashed_rows)} finalized row(s) exist but no persistent hash "
                        f"index file was ever created -- cross-batch dedup will not see them"
                    )

        return ok, problems

    def _report(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        ok_notes, problems = self._run_checks(ctx)

        result.files_processed = len(ok_notes) + len(problems)
        result.files_changed = len(ok_notes)
        result.files_errored = len(problems)

        for n in ok_notes:
            result.notes.append(f"  OK    {n}")
        for p in problems:
            result.notes.append(f"  FAIL  {p}")
            result.errors.append(p)
            logger.error("[audit] %s", p)

        if problems:
            result.success = False
            result.notes.append(
                f"AUDIT FAILED: {len(problems)} problem(s) found. "
                f"Do NOT wipe the DB until these are resolved."
            )
        else:
            result.notes.append("AUDIT PASSED: safe to snapshot and wipe the DB.")

        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        # Audit is inherently read-only -- dry_run and run are identical.
        return self._report(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._report(ctx, dry_run=False)
