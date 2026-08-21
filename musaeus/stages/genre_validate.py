#!/usr/bin/env python3
"""
MUSAEUS — GenreValidate Stage

The one piece of ORPHEUS's retired CrewAI agent crew worth carrying over:
its "Genre Validation Specialist", which checked that artist->genre
assignments agreed with MasterLaw.csv. The crew's other two agents (code
reviewer, code fixer) are already covered by ruff, mypy, 636 tests and
CodeRabbit on every PR; this one had no equivalent, and the library had
11,901 genre-tagged files with nothing validating them.

Rebuilt natively rather than ported: the crew needed CrewAI, Mem0, a
qdrant vector store and an OmniRoute LLM router, none of which still
exist here, and none of which a table lookup requires.

Two jobs, kept deliberately separate because they carry different risk:

  FILL  -- the row has no genre and MasterLaw knows the artist. Additive,
           nothing is overwritten, so this runs by default. 881 rows on
           the live library at build time.

  FLAG  -- the row has a genre that disagrees with MasterLaw (e.g. AC/DC
           filed as "Rock" where the law says "Hard Rock"). REPORT ONLY.
           Never auto-corrected, following the artist-canon precedent and
           Grey's standing rule that these are judgement calls: "Rock" is
           not wrong for AC/DC, it is just less specific, and which one
           is right is the owner's decision, not a table's.

Separator note: the library stores "Disco-Electronic" where MasterLaw
says "Disco/Electronic", because Sanitize strips "/" for filesystem
safety. GenreLaw._norm folds that difference. Skipping it reports ~1,000
conflicts that are pure formatting.
"""

from __future__ import annotations

import logging

from ..canon.genre_law import GenreLaw
from ..context import RunContext, StageResult
from .base import BaseStage

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 200


class GenreValidateStage(BaseStage):
    """Validate library genres against the artist->genre law."""

    NAME = "genre-validate"

    def _law(self, ctx: RunContext) -> GenreLaw:
        return GenreLaw(ctx.config.meta_dir / "MasterLaw.csv")

    def validate(self, ctx: RunContext) -> None:
        law = self._law(ctx)
        logger.info("[genre-validate] MasterLaw knows %d artist(s)", len(law))

    def _check(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        law = self._law(ctx)

        if not len(law):
            result.notes.append("no MasterLaw.csv in MetaData -- nothing to validate against")
            ctx.record_stage(result)
            return result

        rows = ctx.conn.execute(
            "SELECT rowid AS rid, file_path, artist, genre FROM archive "
            "WHERE status='CATALOGUED' ORDER BY artist, album, track"
        ).fetchall()
        result.files_processed = len(rows)

        filled = conflicts = unknown = agreed = 0
        # Keyed by (artist, library genre, law genre), not per file. A
        # conflict is a property of the ARTIST: the first run printed the
        # same "Ac-dc: 'Rock' vs law 'Hard Rock'" line 25 times and filled
        # the report with nothing, because AC/DC has 78 tracks. Grouping
        # turns 375 file-level conflicts into the handful of actual
        # decisions there are to make.
        conflict_groups: dict[tuple[str, str, str], int] = {}

        for i, row in enumerate(rows, 1):
            artist = (row["artist"] or "").strip()
            genre = (row["genre"] or "").strip()
            law_genre = law.genre_for(artist)

            if law_genre is None:
                unknown += 1
                continue

            if not genre:
                filled += 1
                if not dry_run:
                    ctx.conn.execute(
                        "UPDATE archive SET genre = ? WHERE rowid = ?",
                        (law_genre, row["rid"]),
                    )
                    ctx.log_event(
                        "GENRE_FILLED",
                        file_path=row["file_path"],
                        stage=self.NAME,
                        new_value=law_genre,
                        note=f"empty genre filled from MasterLaw ({artist})",
                    )
                continue

            if law.agrees(artist, genre):
                agreed += 1
            else:
                # Report only -- see module docstring.
                conflicts += 1
                key = (artist, genre, law_genre)
                conflict_groups[key] = conflict_groups.get(key, 0) + 1

            if not dry_run and i % _COMMIT_EVERY == 0:
                ctx.conn.commit()

        if not dry_run:
            ctx.conn.commit()

        result.files_changed = filled
        verb = "would fill" if dry_run else "filled"
        result.notes.append(f"MasterLaw artists: {len(law)}")
        result.notes.append(f"  genre agrees:            {agreed}")
        result.notes.append(f"  {verb} empty genre:       {filled}")
        result.notes.append(f"  artist unknown to law:   {unknown}")
        result.notes.append(
            f"  CONFLICTS (report only): {conflicts} file(s) "
            f"across {len(conflict_groups)} artist(s)"
        )
        if conflict_groups:
            result.notes.append("  decisions to make (most files first):")
            ranked = sorted(conflict_groups.items(), key=lambda kv: (-kv[1], kv[0][0]))
            for (artist, have, law_says), n in ranked[:40]:
                result.notes.append(
                    f"    {artist}: {have!r} vs law {law_says!r}  ({n} file{'s' if n != 1 else ''})"
                )
            if len(ranked) > 40:
                result.notes.append(f"    ... and {len(ranked) - 40} more artist(s)")

        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._check(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._check(ctx, dry_run=False)
