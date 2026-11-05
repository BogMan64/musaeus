#!/usr/bin/env python3
"""
MUSAEUS — Stage: HoldExpiry
Delete what has been set aside for 30 days.

Why this exists
---------------
Grey, 2026-11-05: REVIEW and QUARANTINE were checked by hand while MUSAEUS
matured, and that stops. MUSAEUS makes the call. The hold is what is left of
the check: a wrong call stays one move from undone for 30 days. Deleting at
once was weighed and refused the same day -- the run of 2026-11-04 wrongly set
aside 6 of 112 tribute songs, and a dupe rule once took Synchronicity I (#270).

What it deletes
---------------
Audio older than HOLD_DAYS in REVIEW/DUPES_MOVED, REVIEW/TRIBUTE_REMOVED,
QUARANTINE/denied and QUARANTINE/corrupted, with its row. Manifests and
restore scripts stay: they are small, and they are the record of what went.

What it refuses, however old
----------------------------
A file no held row points at: nothing says what it is. A dupe loser whose
keeper has left the library: it may be the last copy. A tribute song on
tribute_keep.tsv: Grey said keep, and only the file has yet to move back. A
file named in MetaData/hold_keep.txt: Grey said stay (2026-11-05, the 34 denied
files with no proof of another copy).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from ..config import AUDIO_EXTENSIONS
from ..context import RunContext, StageResult
from ..db import deny_hash, ensure_deny_list, open_hash_index
from .base import BaseStage
from .organize import _remove_emptied_dirs
from .tribute_quarantine import _kept_songs, _song_key

logger = logging.getLogger(__name__)

HOLD_DAYS = 30

KEEP_FILENAME = "hold_keep.txt"

_HELD_STATUSES = ("DUPE_REVIEW", "TRIBUTE_REVIEW", "QUARANTINED")


def _kept_files(meta_dir: Path) -> set[str]:
    """File names that stay, one per line, exactly as they are on disk."""
    path = meta_dir / KEEP_FILENAME
    return set(path.read_text(encoding="utf-8").splitlines()) if path.is_file() else set()


def _expired(root: Path, cutoff: float) -> list[Path]:
    """Audio under *root* that came to rest before *cutoff*.

    ctime, because a move keeps mtime and sets ctime, so it dates the arrival
    in the hold. Anything else that touches a file only lengthens its hold.
    """
    return sorted(
        p
        for p in root.rglob("*")
        if p.suffix.lower() in AUDIO_EXTENSIONS and p.is_file() and p.stat().st_ctime < cutoff
    )


def _keeper_in_library(conn, audio_hash: str | None) -> bool:
    """True when this audio, or the copy kept over it, is still in the library.

    By audio, not path: `duplicates` keeps the path a keeper had when its group
    was decided, and Canonicalize and Finalize have moved it since. The file is
    looked for as well: Audit only reports a row whose file has gone, so a
    CATALOGUED row alone does not prove a copy survives (CodeRabbit, #286).
    """
    rows = conn.execute(
        """
        SELECT file_path FROM archive
         WHERE status = 'CATALOGUED'
           AND audio_hash IN (
               SELECT ?
               UNION
               SELECT keeper.audio_hash
                 FROM duplicates loser
                 JOIN duplicates keeper
                   ON keeper.group_id = loser.group_id AND keeper.status = 'keep'
                WHERE loser.audio_hash = ?
           )
        """,
        (audio_hash, audio_hash),
    ).fetchall()
    return any(Path(row["file_path"]).is_file() for row in rows)


class HoldExpiryStage(BaseStage):
    """Delete audio held in REVIEW and QUARANTINE past HOLD_DAYS."""

    NAME = "hold-expiry"

    def validate(self, ctx: RunContext) -> None:
        """Nothing to check: a folder that is absent simply holds nothing."""

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._process(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._process(ctx, dry_run=False)

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """A file logged as deleted must really be gone."""
        rows = ctx.conn.execute(
            "SELECT file_path FROM events WHERE event_type = 'HOLD_EXPIRED' AND run_id = ?",
            (ctx.run_id,),
        ).fetchall()
        alive = [Path(r["file_path"]).name for r in rows if Path(r["file_path"]).exists()]
        if alive:
            return [f"{len(alive)} file(s) logged as deleted are on disk: {', '.join(alive[:3])}"]
        return []

    def _refusal(self, ctx: RunContext, row, kept: set[tuple[str, str]]) -> str:
        """Why this held file must stay, or "" when it may go."""
        if row is None or row["status"] not in _HELD_STATUSES:
            return "no held record points at it"
        if row["status"] == "DUPE_REVIEW" and not _keeper_in_library(ctx.conn, row["audio_hash"]):
            return "the copy kept over it has left the library"
        if row["status"] == "TRIBUTE_REVIEW" and _song_key(row["artist"], row["title"]) in kept:
            return "it is on tribute_keep.tsv"
        return ""

    def _delete(self, ctx: RunContext, ledger, row, path: Path, deny: bool) -> None:
        """Records first, then the file, then commit, as DenyListStage moves: a
        delete cannot be rolled back and a DB write can (CodeRabbit, #286). A
        crash before the commit leaves a row whose file is gone, which Ghost finds.
        """
        reason = f"held {HOLD_DAYS} days after {row['status']}"
        # Audio another row carries is not denied: that would refuse the survivor.
        h = row["audio_hash"]
        if (
            deny
            and h
            and not ctx.conn.execute(
                "SELECT 1 FROM archive WHERE audio_hash = ? AND id != ? LIMIT 1", (h, row["id"])
            ).fetchone()
        ):
            deny_hash(ledger, h, reason, str(path))
        ctx.log_event("HOLD_EXPIRED", file_path=str(path), stage=self.NAME, note=reason)
        ctx.conn.execute("DELETE FROM archive_tier_hashes WHERE path = ?", (str(path),))
        ctx.conn.execute("DELETE FROM archive WHERE id = ?", (row["id"],))
        try:
            path.unlink()
        except OSError:
            ledger.rollback()
            ctx.conn.rollback()
            raise
        ledger.commit()
        ctx.conn.commit()

    def _process(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        cfg = ctx.config
        corrupted = cfg.quarantine / "corrupted"
        roots = (
            cfg.dupes_review_dir,
            cfg.tribute_review_dir,
            cfg.quarantine / "denied",
            corrupted,
        )
        cutoff = time.time() - HOLD_DAYS * 86400
        expired = [(p, root) for root in roots if root.is_dir() for p in _expired(root, cutoff)]
        if not expired:
            result.notes.append(f"nothing held past {HOLD_DAYS} days")
            ctx.record_stage(result)
            return result

        kept = _kept_songs(cfg.meta_dir)
        kept_files = _kept_files(cfg.meta_dir)
        # Not in a preview: opening the ledger can create it (CodeRabbit, #286).
        ledger = None if dry_run else open_hash_index(cfg.hash_index_path)
        if ledger is not None:
            ensure_deny_list(ledger)
        try:
            for path, root in expired:
                result.files_processed += 1
                row = ctx.conn.execute(
                    "SELECT id, status, artist, title, audio_hash FROM archive WHERE file_path = ?",
                    (str(path),),
                ).fetchone()
                why = (
                    f"it is on {KEEP_FILENAME}"
                    if path.name in kept_files
                    else self._refusal(ctx, row, kept)
                )
                if why:
                    result.files_skipped += 1
                    result.notes.append(f"kept past the hold: {path.name} -- {why}")
                    continue
                result.files_changed += 1
                if ledger is None:
                    result.notes.append(f"[DRY] would delete {path.name}")
                    continue
                try:
                    # A damaged file is not denied: its replacement is wanted.
                    self._delete(ctx, ledger, row, path, deny=root != corrupted)
                except OSError as exc:
                    result.success = False
                    result.files_changed -= 1
                    result.files_errored += 1
                    result.errors.append(f"could not delete {path}: {exc}")
                    continue
                _remove_emptied_dirs(path.parent, root)
                logger.info("[hold-expiry] deleted %s", path)
                result.notes.append(f"deleted {path.name}")
        finally:
            if ledger is not None:
                ledger.close()

        ctx.record_stage(result)
        return result
