#!/usr/bin/env python3
"""
MUSAEUS -- LibraryBake Stage (Act 3, straight after Finalize)

Builds the -18 LUFS listening copy in ALAC_Library from each new master in
ALAC-Archival, and points the catalogue row at that copy. The master is left
exactly as Finalize placed it.

Why it exists. Grey decided on 2026-08-18 that new work lands in the pristine
masters tier and every edition is baked from it. The bake was written for
that (musaeus/library_bake.py, then scripts/alac_library/build_alac_library.py),
but Finalize never switched to the archive, so the bake stayed a manual
second step that an end-to-end run never took. The first batch on the fresh
vault (2026-09-24) ended with no masters tree at all, and doctor warned
"masters tree not found". Finalize now files masters, and this stage makes
the library copies in the same run.

Placed straight after Finalize, before BPM/Forge/Tagger/Organize, so that:
  * the master is never touched again after Finalize files it;
  * Forge measures the copy people actually play (-18 LUFS, so its
    ReplayGain is right for that file), not the master;
  * Organize moves the library copy together with its master
    (see organize.py), keeping master_path_for()'s mirror true.

The convention after this stage is the one every tool already relies on:
archive.file_path is the ALAC_Library copy, and its master is at the same
relative path under ALAC-Archival (editions.master_path_for).
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

# The module, not its names: musaeus.library_bake imports stages.corrupt,
# which loads this package -- binding names here would read a half-built
# module. Attributes are looked up when the stage runs.
from .. import library_bake as bake
from ..context import RunContext, StageResult
from ..deep_scan import ensure_columns as _deep_scan_ensure_columns
from .base import BaseStage, StageError

logger = logging.getLogger(__name__)


class LibraryBakeStage(BaseStage):
    """-18 LUFS ALAC_Library copies from new masters in ALAC-Archival."""

    NAME = "library-bake"

    @classmethod
    def plan_candidates(cls, conn, cfg) -> tuple[int, str]:
        """Rows this stage would act on. Read-only; see planner.py."""
        archive = getattr(cfg, "alac_archive", None)
        if archive is None:
            return 0, "no masters tier configured"
        # A COUNT, tolerant of a catalogue that predates the bake columns --
        # the planner runs against whatever database is there.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(archive)").fetchall()}
        pending = (
            "AND (lufs_baked_at IS NULL OR lufs_baked_at = '')" if "lufs_baked_at" in cols else ""
        )
        n = conn.execute(
            f"SELECT COUNT(*) FROM archive WHERE status = 'CATALOGUED' AND file_path LIKE ? || '%' {pending}",
            (str(archive),),
        ).fetchone()[0]
        return n, f"new masters needing a {bake.TARGET_I} LUFS library copy"

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """Each copy baked this run must exist, be the row's file, and still
        have its master at the mirrored path -- the convention the stage
        exists to establish. Checked from this run's LUFS_BAKE events rather
        than from the result's counters, which only say what was attempted."""
        rows = ctx.conn.execute(
            "SELECT e.new_value, a.file_path FROM events e LEFT JOIN archive a ON a.file_path = e.new_value "
            "WHERE e.run_id = ? AND e.event_type = 'LUFS_BAKE'",
            (ctx.run_id,),
        ).fetchall()
        problems = []
        lib, arch = ctx.alac_library, ctx.config.alac_archive
        for baked, row_path in rows:
            copy = Path(baked)
            if row_path is None:
                problems.append(f"baked copy is no row's file: {copy.name}")
            elif not copy.is_file():
                problems.append(f"baked copy missing on disk: {copy.name}")
            elif copy.is_relative_to(lib) and not (arch / copy.relative_to(lib)).is_file():
                problems.append(f"baked copy has no master behind it: {copy.name}")
        return problems

    def validate(self, ctx: RunContext) -> None:
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            raise StageError(
                "ffmpeg/ffprobe not found -- the bake measures and re-encodes with them"
            )

    def _bake(self, ctx: RunContext, execute: bool) -> StageResult:
        result = self._make_result(dry_run=not execute)
        archive_dir, library_dir = ctx.config.alac_archive, ctx.alac_library
        _deep_scan_ensure_columns(ctx.conn)  # the decode gate reads these columns
        rows = bake._candidate_rows(ctx.conn, archive_dir, force=False)
        result.notes.append(f"new masters to bake: {len(rows)}")
        for row in rows:
            msg = bake._process_one(
                ctx.conn, row, archive_dir, library_dir, execute, run_id=ctx.run_id, stage=self.NAME
            )
            result.files_processed += 1
            if msg.startswith(("BAKED", "WOULD BAKE")):
                result.files_changed += 1
            elif msg.startswith("SKIP"):
                result.files_skipped += 1
                result.notes.append(msg)
            else:
                # The master stays where Finalize put it and its row keeps
                # pointing at it; the next run retries. Listed, not fatal.
                result.files_errored += 1
                result.errors.append(msg)
        ctx.conn.commit()
        if rows:
            result.notes.append(
                f"baked {result.files_changed}, skipped {result.files_skipped}, "
                f"failed {result.files_errored} (masters untouched either way)"
            )
        return result

    def run(self, ctx: RunContext) -> StageResult:
        result = self._bake(ctx, execute=True)
        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._bake(ctx, execute=False)
        ctx.record_stage(result)
        return result
