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
    def plan_candidates(cls, conn) -> tuple[int, str]:
        """Rows this stage would act on. Read-only; see planner.py."""
        n = conn.execute("SELECT COUNT(*) FROM archive WHERE status='PENDING'").fetchone()[0]
        return int(n), "pending files awaiting ingest"

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
