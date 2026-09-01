#!/usr/bin/env python3
"""
MUSAEUS — Stage 1: Ingest
Scan the inbox for audio files and register them in the archive.

What it does:
  - Walks MUSAEUS_INBOX recursively
  - Accepts only files with recognised AUDIO_EXTENSIONS
  - Skips files already in the archive (by file_path) — idempotent
  - Registers new files with status='PENDING' and basic fs metadata
  - Logs an INGEST event for every new file
  - dry_run() lists what WOULD be ingested, zero DB changes

What it does NOT do:
  - Hash files (that's Sentinel)
  - Read tags (that's Scholar)
  - Move / rename files
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from ..config import AUDIO_EXTENSIONS
from ..context import RunContext, StageResult
from ..db import upsert_archive
from .base import BaseStage, StageError

logger = logging.getLogger(__name__)


def _utc_mtime(path: Path) -> str:
    ts = os.path.getmtime(path)
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _scan_inbox(inbox: Path) -> list[Path]:
    """Walk inbox, return all audio files sorted by path."""
    found: list[Path] = []
    if not inbox.exists():
        return found
    for root, _dirs, files in os.walk(inbox):
        for fname in files:
            p = Path(root) / fname
            if p.suffix.lower() in AUDIO_EXTENSIONS:
                found.append(p)
    return sorted(found)


def _known_paths(conn) -> set[str]:  # type: ignore[type-arg]
    """Return the set of file_path strings already in the archive."""
    rows = conn.execute("SELECT file_path FROM archive").fetchall()
    return {row["file_path"] for row in rows}


class IngestStage(BaseStage):
    """
    Stage 1 — Ingest inbox audio files into the archive.
    """

    @classmethod
    def plan_candidates(cls, conn, cfg) -> tuple[int, str]:
        """INBOX files with no archive row — the ones ingest would add.

        Counted from the filesystem, not from archive rows: a file that has
        never been scanned has no row yet, so a row count reports 0 while
        the inbox is full. That is exactly what the first version did.

        The SECOND version over-corrected into `waiting + pending`, which
        breaks the planner's contract -- "items this stage would act on" --
        in both directions at once. run() skips every path already in the
        archive, so the PENDING rows are not work; and on 2026-09-01 all
        10,489 of them were files still sitting in INBOX, so they were
        already inside `waiting` and were added to themselves. The preview
        offered 30,257 for a run that ingested 9,279.

        Nothing it said was false -- there really were 19,768 files in
        INBOX and 10,489 PENDING rows. Only the sum meant nothing, and the
        sum is the number a person reads.

        So: ask the same question run() asks, with the same definition of
        "known", and report the overlap rather than folding it in.
        """
        inbox = getattr(cfg, "inbox", None)
        if inbox is None or not Path(inbox).exists():
            return 0, "INBOX does not exist — nothing to ingest"

        found = _scan_inbox(Path(inbox))
        known = _known_paths(conn)
        new_files = sum(1 for p in found if str(p) not in known)
        already = len(found) - new_files

        if already:
            return new_files, (
                f"new files in INBOX ({new_files}); "
                f"{already} already have rows and would be skipped"
            )
        return new_files, "new files in INBOX"

    NAME = "ingest"

    # ── Validate ──────────────────────────────────────────────────────────────

    def validate(self, ctx: RunContext) -> None:
        if not ctx.inbox.exists():
            raise StageError(
                f"Inbox directory does not exist: {ctx.inbox}\n"
                f"Create it or set MUSAEUS_INBOX to an existing path."
            )

    # ── Dry run ───────────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=True)
        known = _known_paths(ctx.conn)
        candidates = _scan_inbox(ctx.inbox)

        new_files: list[Path] = []
        for p in candidates:
            result.files_processed += 1
            if str(p) in known:
                result.files_skipped += 1
                continue
            new_files.append(p)
            result.files_changed += 1

        if new_files:
            result.notes.append(f"Would ingest {len(new_files)} new file(s):")
            for p in new_files[:20]:
                result.notes.append(f"  + {p.relative_to(ctx.inbox)}")
            if len(new_files) > 20:
                result.notes.append(f"  ... and {len(new_files) - 20} more")
        else:
            result.notes.append("No new audio files found in inbox.")

        ctx.record_stage(result)
        return result

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)
        known = _known_paths(ctx.conn)
        candidates = _scan_inbox(ctx.inbox)

        _COMMIT_EVERY = 100

        for path in candidates:
            result.files_processed += 1
            path_str = str(path)

            if path_str in known:
                result.files_skipped += 1
                logger.debug("skip (known): %s", path.name)
                continue

            try:
                stat = path.stat()
                row = {
                    "file_path": path_str,
                    "filename": path.name,
                    "ext": path.suffix.lower(),
                    "size_bytes": stat.st_size,
                    "last_modified": _utc_mtime(path),
                    "status": "PENDING",
                }
                upsert_archive(ctx.conn, row)
                ctx.log_event(
                    "INGEST",
                    file_path=path_str,
                    stage=self.NAME,
                    note=f"size={stat.st_size}",
                )
                result.files_changed += 1
                logger.info("ingested: %s", path.name)

            except OSError as exc:
                result.files_errored += 1
                result.errors.append(f"{path.name}: {exc}")
                logger.warning("ingest error: %s — %s", path.name, exc)

            if result.files_processed % _COMMIT_EVERY == 0:
                ctx.conn.commit()
                logger.info("[ingest] checkpoint %d files", result.files_processed)

        if result.files_errored > 0:
            result.success = False

        ctx.record_stage(result)
        return result
