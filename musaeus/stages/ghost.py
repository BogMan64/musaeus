#!/usr/bin/env python3
"""
MUSAEUS — Stage: Ghost
Sweep the archive for files that no longer exist on disk.

What it does:
  - Queries every file_path in the archive
  - Checks whether each path still exists on disk
  - Marks missing files as status='GHOST' in the archive
  - Logs a GHOST_FOUND event for every new ghost discovered
  - dry_run() reports all ghosts without any DB changes
  - Re-run safe: already-GHOST rows are reported but not double-logged

Why it matters:
  - Files get moved, renamed, or deleted outside Musaeus
  - GHOST rows are excluded from pipeline stages automatically
  - run `musaeus ghost` after any external library reorganisation
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..context import RunContext, StageResult
from .base import BaseStage

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 200


class GhostStage(BaseStage):
    """
    Ghost sweep — mark archive entries whose files no longer exist.
    """

    NAME = "ghost"

    # ── Validate ──────────────────────────────────────────────────────────────

    def validate(self, ctx: RunContext) -> None:
        total = ctx.conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
        if total == 0:
            logger.info("[ghost] archive is empty — nothing to sweep")

    # ── Dry run ───────────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=True)
        rows = ctx.conn.execute(
            "SELECT file_path, status FROM archive ORDER BY file_path"
        ).fetchall()

        ghosts: list[str] = []
        for row in rows:
            result.files_processed += 1
            if not Path(row["file_path"]).exists():
                ghosts.append(row["file_path"])

        result.files_changed = len(ghosts)
        if ghosts:
            result.notes.append(f"Would mark {len(ghosts)} ghost(s):")
            for p in ghosts[:20]:
                result.notes.append(f"  ✗ {p}")
            if len(ghosts) > 20:
                result.notes.append(f"  ... and {len(ghosts) - 20} more")
        else:
            result.notes.append("No ghosts found — all archive files present on disk.")

        ctx.record_stage(result)
        return result

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)
        rows = ctx.conn.execute(
            "SELECT file_path, status FROM archive ORDER BY file_path"
        ).fetchall()

        new_ghosts = 0
        already_ghost = 0

        for row in rows:
            path_str = row["file_path"]
            result.files_processed += 1

            if Path(path_str).exists():
                result.files_skipped += 1
                continue

            # File is missing
            if row["status"] == "GHOST":
                already_ghost += 1
                result.files_skipped += 1
                continue

            # New ghost — mark it
            ctx.conn.execute(
                "UPDATE archive SET status='GHOST', last_seen=datetime('now') WHERE file_path=?",
                (path_str,),
            )
            ctx.log_event(
                "GHOST_FOUND",
                file_path=path_str,
                old_value=row["status"],
                new_value="GHOST",
                stage=self.NAME,
            )
            result.files_changed += 1
            new_ghosts += 1
            logger.info("ghost: %s", path_str)

            if result.files_processed % _COMMIT_EVERY == 0:
                ctx.conn.commit()
                logger.info("[ghost] checkpoint %d", result.files_processed)

        if new_ghosts:
            result.notes.append(f"Marked {new_ghosts} new ghost(s).")
        if already_ghost:
            result.notes.append(f"{already_ghost} file(s) were already GHOST.")
        if new_ghosts == 0 and already_ghost == 0:
            result.notes.append("No ghosts found — all archive files present on disk.")

        ctx.record_stage(result)
        return result
