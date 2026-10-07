"""
MUSAEUS -- Act 1: name untagged files by their sound.

Scholar names a file with no tags from an "Artist - Title" file name. A file
it cannot name that way -- "03 - Yesterday.m4a", which says the title but not
the artist -- is identified here from its audio: fpcalc makes a fingerprint,
AcoustID answers with recordings, and acousticid.name_from_results takes a
name only when a recording agrees with the title the file name gives and
every agreeing recording names the same artist (Grey, 2026-09-25).

Right after Scholar, so Normalize, consolidation, tribute detection and the
genre step all see the name before Act 3 files the track.

It never fails Act 1. No key, no fpcalc, no network: the row stays unnamed,
is reported here, and Scholar's own check has already listed it for Grey.

The fingerprint is stored in the columns AcousticIDStage owns, so the
enrichment pass reuses it; acousticid_checked_at is NOT stamped, because
that pass must still look for acoustic duplicates of this row.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..context import RunContext, StageResult
from . import acousticid
from .base import BaseStage
from .scholar import title_hint

logger = logging.getLogger(__name__)


def _nameless(ctx: RunContext) -> list[dict]:
    rows = ctx.conn.execute(
        "SELECT id, file_path FROM archive WHERE status = 'CATALOGUED' "
        "AND COALESCE(TRIM(artist), '') = '' AND COALESCE(TRIM(title), '') = '' "
        "ORDER BY file_path"
    ).fetchall()
    return [dict(r) for r in rows]


class AcoustIDNameStage(BaseStage):
    """Name files that have no tags and no usable file name, from their sound."""

    NAME = "acoustid-name"

    def validate(self, ctx: RunContext) -> None:
        return None

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=True)
        n = len(_nameless(ctx))
        result.notes.append(f"files with no name to look up by their sound: {n}")
        ctx.record_stage(result)
        return result

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """Every row this run named must still carry that name."""
        blank = ctx.conn.execute(
            "SELECT COUNT(*) FROM events e JOIN archive a ON a.file_path = e.file_path "
            "WHERE e.run_id = ? AND e.event_type = 'NAMED_BY_ACOUSTID' "
            "AND (COALESCE(TRIM(a.artist), '') = '' OR COALESCE(TRIM(a.title), '') = '')",
            (ctx.run_id,),
        ).fetchone()[0]
        return [f"{blank} row(s) named by AcoustID are blank again"] if blank else []

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)
        rows = _nameless(ctx)
        if not rows:
            result.notes.append("no nameless files")
            ctx.record_stage(result)
            return result

        key = ctx.config.acousticid_api_key
        if not key:
            result.notes.append(f"{len(rows)} nameless file(s) left for you: no AcoustID key set")
            ctx.record_stage(result)
            return result

        acousticid._ensure_columns(ctx.conn)
        named = refused = no_hint = unavailable = 0
        for row in rows:
            result.files_processed += 1
            path = Path(row["file_path"])
            hint = title_hint(path)
            if not hint:
                no_hint += 1
                continue
            try:
                duration, fingerprint = acousticid._fpcalc(str(path))
            except (RuntimeError, ValueError, OSError) as exc:
                result.notes.append(f"{len(rows)} nameless file(s) left for you: {exc}")
                break
            ctx.conn.execute(
                "UPDATE archive SET chromaprint = ?, chromaprint_duration = ? WHERE id = ?",
                (fingerprint, duration, row["id"]),
            )
            try:
                results = acousticid._acousticid_query(fingerprint, duration, key)
            except acousticid.LookupUnavailable as exc:
                unavailable += 1
                logger.warning("[acoustid-name] lookup unavailable for %s: %s", path.name, exc)
                continue
            found = acousticid.name_from_results(results, hint)
            if found is None:
                refused += 1
                continue
            artist, title, score = found
            ctx.conn.execute(
                "UPDATE archive SET artist = ?, title = ? WHERE id = ?",
                (artist, title, row["id"]),
            )
            ctx.log_event(
                "NAMED_BY_ACOUSTID",
                file_path=str(path),
                stage=self.NAME,
                new_value=f"{artist} - {title}",
                note=(
                    f"no tags; named by its sound (score {score:.2f}, needs "
                    f"{acousticid.NAMING_MIN_SCORE:.2f}), confirmed by the title "
                    f"in the file name {hint!r}"
                ),
            )
            named += 1
            result.files_changed += 1
        ctx.conn.commit()

        result.notes.append(f"nameless files: {len(rows)}; named by their sound: {named}")
        if refused:
            result.notes.append(f"  no single agreeing recording, left for you: {refused}")
        if no_hint:
            result.notes.append(
                f"  no title in the file name to confirm with, left for you: {no_hint}"
            )
        if unavailable:
            result.notes.append(f"  AcoustID could not be reached, left for you: {unavailable}")
        ctx.record_stage(result)
        return result
