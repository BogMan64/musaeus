#!/usr/bin/env python3
"""
MUSAEUS — Stage: Identity Tag

Persist recording identity (MusicBrainz / AcoustID) into the audio files
themselves, so it survives a musaeus.db wipe.

Runs after mb_enrich, because it writes what mb_enrich resolved.

Design notes, all of them earned on 2026-08-26
----------------------------------------------
  - A completion marker (`identity_tagged_at`) records that the WRITE WAS
    ATTEMPTED AND VERIFIED, never merely attempted. Selection is on
    `identity_tagged_at IS NULL`, so a marker written on a failed write
    would remove the row from the queue for ever. mb_enrich lost 2,328
    rows' worth of work to exactly that mistake in two different forms in
    one day; the rule here is that only a proven write settles a row.

  - A file whose write could not be verified is left UNMARKED and retried
    next run. That is the correct outcome, not an error to be counted
    away.

  - verify_effect re-reads tags from DISK rather than re-reading what this
    stage believes it wrote, so a stage that tagged nothing cannot satisfy
    its own check.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from ..context import RunContext, StageResult
from ..identity_tags import IDENTITY_FIELDS, read_identity, write_identity
from .base import BaseStage

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 50
_MARKER = "identity_tagged_at"


def _ensure_columns(conn) -> None:  # type: ignore[type-arg]
    existing = {row[1] for row in conn.execute("PRAGMA table_info(archive)").fetchall()}
    if _MARKER not in existing:
        conn.execute(f"ALTER TABLE archive ADD COLUMN {_MARKER} TEXT")
        conn.commit()


class IdentityTagStage(BaseStage):
    """Write MusicBrainz/AcoustID identifiers into the files."""

    NAME = "identity-tag"

    def validate(self, ctx: RunContext) -> None:
        try:
            n = ctx.conn.execute(
                "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' "
                "AND mb_artist_id IS NOT NULL"
            ).fetchone()[0]
        except Exception:
            n = 0
        logger.info("[identity-tag] %d row(s) carry an identity to persist", n)

    @staticmethod
    def _present(conn) -> list[str]:  # type: ignore[no-untyped-def]
        """Which identity columns this database actually has.

        Not every column exists on every database: mb_* are auto-migrated
        by mb_enrich on its first run, acousticid_* by AcousticIDStage.
        Selecting a column that is absent made the whole stage bail with
        "nothing to do" -- a stage that silently does nothing because one
        optional column is missing is the failure mode this project keeps
        finding, so take the intersection instead.
        """
        have = {r[1] for r in conn.execute("PRAGMA table_info(archive)").fetchall()}
        return [c for c in IDENTITY_FIELDS if c in have]

    def _rows(self, ctx: RunContext, force: bool = False):  # type: ignore[no-untyped-def]
        present = self._present(ctx.conn)
        if not present:
            return []
        cols = ", ".join(present)
        identity_any = " OR ".join(f"{c} IS NOT NULL" for c in present)
        where = "" if force else f" AND ({_MARKER} IS NULL OR {_MARKER}='')"
        return ctx.conn.execute(
            f"""
            SELECT id, file_path, {cols} FROM archive
             WHERE status='CATALOGUED'
               AND ({identity_any})
               {where}
             ORDER BY file_path
            """
        ).fetchall()

    def _process(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        if not dry_run:
            _ensure_columns(ctx.conn)
        present = self._present(ctx.conn)
        if not present:
            result.notes.append("no identity columns in this database yet — nothing to do")
            ctx.record_stage(result)
            return result
        rows = self._rows(ctx, force=bool(ctx.get("identity_tag_force", False)))

        result.notes.append(f"files with identity to persist: {len(rows)}")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        unverified = 0

        for row in rows:
            result.files_processed += 1
            path = Path(row["file_path"])
            values = {c: row[c] for c in present if row[c]}
            if not values:
                result.files_skipped += 1
                continue
            if dry_run:
                result.files_changed += 1
                continue
            if not path.exists():
                result.files_errored += 1
                result.errors.append(f"{path.name}: missing on disk")
                continue

            ok, detail = write_identity(path, values)
            if not ok:
                # Unverified: leave the marker NULL so it is retried. A
                # marker here would settle the row on a write that never
                # landed -- the exact shape of silent-no-op #2.
                unverified += 1
                result.files_skipped += 1
                logger.warning("[identity-tag] not verified for %s (%s)", path.name, detail)
                continue

            ctx.conn.execute(
                f"UPDATE archive SET {_MARKER}=? WHERE id=?", (now, row["id"])
            )
            ctx.log_event(
                "IDENTITY_TAGGED",
                file_path=str(path),
                stage=self.NAME,
                note=f"{len(values)} tag(s): {', '.join(sorted(values))}",
            )
            result.files_changed += 1
            if result.files_processed % _COMMIT_EVERY == 0:
                ctx.conn.commit()

        if not dry_run:
            ctx.conn.commit()
        verb = "Would write" if dry_run else "Wrote"
        result.notes.append(f"{verb} identity tags to {result.files_changed} file(s).")
        if unverified:
            result.notes.append(
                f"  {unverified} file(s) could NOT be verified on disk — "
                f"left unmarked, will be retried next run."
            )
        ctx.record_stage(result)
        return result

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """Read the tags back off DISK for a sample of what we just marked."""
        problems: list[str] = []
        if result.dry_run or not result.files_changed:
            return problems
        if "mb_artist_id" not in self._present(ctx.conn):
            return problems
        rows = ctx.conn.execute(
            f"SELECT file_path, mb_artist_id FROM archive "
            f"WHERE {_MARKER} IS NOT NULL AND mb_artist_id IS NOT NULL "
            f"ORDER BY {_MARKER} DESC LIMIT 5"
        ).fetchall()
        for row in rows:
            on_disk = read_identity(Path(row["file_path"])).get("mb_artist_id")
            if on_disk != row["mb_artist_id"]:
                problems.append(
                    f"marked as tagged but the file does not carry it: {row['file_path']}"
                )
        return problems

    def run(self, ctx: RunContext) -> StageResult:
        return self._process(ctx, dry_run=False)

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._process(ctx, dry_run=True)
