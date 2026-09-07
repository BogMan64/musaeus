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
from ..context import RunContext, StageResult, elision
from .base import BaseStage

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 200


class GenreValidateStage(BaseStage):
    """Validate library genres against the artist->genre law."""

    @classmethod
    def plan_candidates(cls, conn, cfg) -> tuple[int, str]:
        """Rows this stage would act on. Read-only; see planner.py."""
        n = conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' AND (genre IS NULL OR trim(genre)='')"
        ).fetchone()[0]
        return int(n), "files with no genre that the law could fill"

    NAME = "genre-validate"

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """Assert the two rules this stage exists to enforce still hold.

        One genre per artist (never per song) is Grey's standing rule, and
        it is the one a partial write breaks most quietly: consolidating
        900 rows and missing 3 leaves an artist straddling two genres, which
        no count in the result would reveal. Checked with a GROUP BY rather
        than by re-reading what we just wrote, so a no-op cannot pass.
        """
        problems: list[str] = []
        multi = ctx.conn.execute(
            "SELECT artist, COUNT(DISTINCT genre) AS n FROM archive "
            "WHERE status='CATALOGUED' AND genre IS NOT NULL AND trim(genre)!='' "
            "GROUP BY artist HAVING n > 1 ORDER BY n DESC"
        ).fetchall()
        if multi:
            names = ", ".join(f"{r['artist']} ({r['n']})" for r in multi[:3])
            problems.append(
                f"{len(multi)} artist(s) still carry more than one genre after "
                f"genre-validate claimed {result.files_changed} change(s): {names}"
            )

        # Items are repr'd, not joined raw: a genre VALUE can itself contain
        # ", " -- the live library holds one called 'Pop, Rock' across 72
        # tracks -- so a bare join renders two findings as three and sends
        # the reader looking for a genre that was never missing. Line 137
        # already had this right; these two did not.
        # A genre outside the closed vocabulary means the law was not applied,
        # or something wrote around it.
        law = self._law(ctx)
        if len(law):
            stray = ctx.conn.execute(
                "SELECT DISTINCT genre FROM archive WHERE status='CATALOGUED' "
                "AND genre IS NOT NULL AND trim(genre)!=''"
            ).fetchall()
            # permits() folds "/" against "-", so "R&B/Funk/Soul" is not
            # reported as stray merely because the law spells it with a dash.
            off = [r["genre"] for r in stray if not law.permits(r["genre"])]
            if off:
                problems.append(
                    f"{len(off)} genre(s) outside the closed vocabulary: "
                    + ", ".join(repr(g) for g in off[:5])
                )

            # ...and permits() cannot catch a value the law itself contains.
            # Measured 2026-08-24: "Electronic/Dance" (64 tracks) and
            # "Classic Rock" (11) sat in the library, absent from
            # Genre_Allowed.txt, while Genre_Canonical_Map.txt already held
            # rules mapping both to canonical values. ScholarStage writes the
            # embedded genre tag verbatim and never consults GenreCanon;
            # EnrichStage only fills EMPTY genres, so nothing revisited them;
            # and once the value reached MasterLaw, permits() blessed it.
            # Checking against the hand-written vocabulary is the only link
            # in that chain that can actually fail.
            allowed = self._allowed_vocabulary(ctx)
            if allowed:
                unlisted = sorted(
                    {r["genre"] for r in stray} - allowed,
                    key=str.lower,
                )
                if unlisted:
                    problems.append(
                        f"{len(unlisted)} genre(s) in use but absent from "
                        "Genre_Allowed.txt: "
                        + ", ".join(repr(g) for g in unlisted[:5])
                    )

            # permits() is deliberately forgiving, which leaves a gap: a stored
            # "pop" satisfies it because the law spells the same genre "Pop".
            # The value is then legal but not canonical, and it hides from
            # every check above -- found 2026-08-23 on a single track
            # (Gwen Stefani, "Hollaback Girl") that had sat there through
            # multiple clean audits. Compare exact spelling separately so a
            # case or punctuation variant is reported rather than absorbed.
            canon = {g: g for g in law.genres}
            variants = [
                r["genre"]
                for r in stray
                if r["genre"] not in canon and law.permits(r["genre"])
            ]
            if variants:
                problems.append(
                    f"{len(variants)} genre(s) legal but not spelled canonically: "
                    + ", ".join(f"{v!r}" for v in variants[:5])
                )
        return problems

    def _law(self, ctx: RunContext) -> GenreLaw:
        return GenreLaw(ctx.config.meta_dir / "MasterLaw.csv")

    @staticmethod
    def _canon(ctx: RunContext):
        """GenreCanon, or None when the vault has no canon files."""
        if getattr(ctx, "config", None) is None:
            return None
        from ..canon.genre import GenreCanon

        allowed = ctx.config.meta_dir / "Genre_Allowed.txt"
        mapping = ctx.config.meta_dir / "Genre_Canonical_Map.txt"
        if not allowed.exists():
            return None
        return GenreCanon(allowed, mapping)

    @staticmethod
    def _allowed_vocabulary(ctx: RunContext) -> set[str]:
        """The vocabulary from Genre_Allowed.txt, or empty if absent.

        This is the only genre check in the project that is not
        self-certifying. `GenreLaw.genres` is `set(self._map.values())` --
        derived from MasterLaw's own contents -- so any value that reaches
        the law becomes vocabulary and `permits()` returns True for it
        forever. `Genre_Allowed.txt` is written by hand and is the file
        GenreCanon actually enforces, so it can disagree with the library,
        which is exactly what makes it worth checking.
        """
        # A context without config (unit tests stubbing the law) has no vault
        # to read a vocabulary from, and an empty set correctly disables the
        # check rather than reporting every genre as unlisted.
        if getattr(ctx, "config", None) is None:
            return set()
        path = ctx.config.meta_dir / "Genre_Allowed.txt"
        if not path.exists():
            return set()
        with open(path, encoding="utf-8") as fh:
            return {ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")}

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

        filled = conflicts = unknown = agreed = illegal_fixed = 0
        illegal_stuck: dict[str, int] = {}
        allowed = self._allowed_vocabulary(ctx)
        canon = self._canon(ctx)
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

            # A genre outside the closed vocabulary is not a disagreement --
            # it is not a genre. Library-vs-law conflicts stay report-only
            # because the library holds the owner's decision (section 4.19),
            # but there is no decision to protect in a value the vocabulary
            # does not contain, and leaving it means a retired genre walks
            # back in on the next ingest.
            #
            # Measured 2026-08-25 on a five-file test batch: "Pop, Rock" --
            # drained to zero and retired the day before -- returned on the
            # very first new file, because ScholarStage writes the file's own
            # genre tag verbatim and never consults GenreCanon. This stage
            # ran and left it, because it only ever filled EMPTY genres.
            if allowed and genre not in allowed:
                replacement = law_genre if law_genre in allowed else None
                if replacement is None:
                    resolved = canon.resolve(genre) if canon else None
                    replacement = resolved if resolved in allowed else None
                if replacement:
                    illegal_fixed += 1
                    if not dry_run:
                        ctx.conn.execute(
                            "UPDATE archive SET genre = ? WHERE rowid = ?",
                            (replacement, row["rid"]),
                        )
                        ctx.log_event(
                            "GENRE_OUTSIDE_VOCABULARY",
                            file_path=row["file_path"],
                            old_value=genre,
                            new_value=replacement,
                            stage=self.NAME,
                            note=f"{genre!r} is not in Genre_Allowed.txt ({artist})",
                        )
                    continue
                illegal_stuck.setdefault(genre, 0)
                illegal_stuck[genre] += 1
                continue

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
        verb2 = "would correct" if dry_run else "corrected"
        result.notes.append(f"  {verb2} genre outside the vocabulary: {illegal_fixed}")
        if illegal_stuck:
            result.notes.append(
                "  OUTSIDE THE VOCABULARY and unresolvable -- these need a ruling:"
            )
            for g, n in sorted(illegal_stuck.items(), key=lambda kv: -kv[1]):
                result.notes.append(f"    {g!r}  ({n} file{'s' if n != 1 else ''})")
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
                result.notes.append(f"    {elision(len(ranked) - 40, unit='artist(s)')}")

        ctx.record_stage(result)
        return result

    def _consolidate(self, ctx: RunContext) -> StageResult:
        """Enforce one genre per ARTIST, not per song.

        Grey's rule, 2026-08-21: genre belongs to the artist. A library
        where Bob Dylan is 84 tracks of "Rock" and 24 of "Folk Rock" is
        not richer for the distinction, it is just harder to browse -- and
        browsing by artist is how this library is actually used.

        Which genre wins, in order:
          1. MasterLaw, where it has an opinion. It is the curated
             authority and the whole point of having one.
          2. Otherwise the artist's dominant genre by file count. Not a
             guess so much as a reading of what the library already
             mostly says -- Rolling Stones at 114 Rock against 2 Blues
             was never really two genres.

        Ties are left ALONE rather than broken arbitrarily: a genuine
        50/50 split is a decision, and this should not invent one.
        """
        result = self._make_result(dry_run=False)
        law = self._law(ctx)

        counts: dict[str, dict[str, int]] = {}
        for row in ctx.conn.execute(
            "SELECT artist, genre, COUNT(*) AS n FROM archive "
            "WHERE status='CATALOGUED' AND genre IS NOT NULL AND trim(genre) != '' "
            "GROUP BY artist, genre"
        ):
            counts.setdefault(row["artist"], {})[row["genre"]] = row["n"]

        by_law = by_majority = ties = changed = 0
        for artist, genres in counts.items():
            if len(genres) < 2:
                continue

            winner = law.genre_for(artist)
            if winner:
                by_law += 1
            else:
                ranked = sorted(genres.items(), key=lambda kv: -kv[1])
                if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
                    ties += 1
                    result.notes.append(
                        f"  TIE, left alone: {artist} " + ", ".join(f"{g}:{n}" for g, n in ranked)
                    )
                    continue
                winner = ranked[0][0]
                by_majority += 1

            cur = ctx.conn.execute(
                "UPDATE archive SET genre = ? WHERE status='CATALOGUED' "
                "AND artist = ? AND genre IS NOT ?",
                (winner, artist, winner),
            )
            changed += cur.rowcount

        # A blank genre is not a disagreement, so the loop above never sees
        # it: `counts` is built only from rows that already HAVE a genre, and
        # the artists it does see are filtered to len(genres) >= 2. So an
        # artist with nothing at all was skipped twice over, while MasterLaw
        # held the answer the whole time -- 33 catalogued tracks on
        # 2026-09-05, among them Gladys Knight (13, R&B/Funk/Soul), Billie
        # Holiday (3, Jazz), Bing Crosby and Dire Straits.
        #
        # Only the LAW fills a blank. Majority is deliberately not used here:
        # consolidating a disagreement toward what the library mostly says is
        # a reading of existing evidence, but inventing a genre for a row
        # that has none would be a guess, and a guess written into the
        # library is indistinguishable from a fact later.
        by_law_blank = 0
        for row in ctx.conn.execute(
            "SELECT DISTINCT artist FROM archive "
            " WHERE status='CATALOGUED' AND artist IS NOT NULL AND trim(artist) != '' "
            "   AND (genre IS NULL OR trim(genre) = '')"
        ).fetchall():
            artist = row["artist"]
            verdict = law.genre_for(artist)
            if not verdict:
                continue
            cur = ctx.conn.execute(
                "UPDATE archive SET genre = ? WHERE status='CATALOGUED' "
                " AND artist = ? AND (genre IS NULL OR trim(genre) = '')",
                (verdict, artist),
            )
            if cur.rowcount:
                by_law_blank += 1
                changed += cur.rowcount
                ctx.log_event(
                    "GENRE_FILLED_FROM_LAW",
                    file_path=artist,
                    new_value=verdict,
                    stage=self.NAME,
                    note="row had no genre; MasterLaw had an opinion",
                )

        ctx.conn.commit()
        result.files_changed = changed
        result.notes.insert(0, f"artists given a genre from MasterLaw: {by_law_blank}")
        result.notes.insert(1, f"artists consolidated by MasterLaw:  {by_law}")
        result.notes.insert(1, f"artists consolidated by majority:   {by_majority}")
        result.notes.insert(2, f"ties left for a human:              {ties}")
        result.notes.insert(3, f"files retagged:                     {changed}")
        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._check(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        if ctx.get("genre_consolidate", False):
            return self._consolidate(ctx)
        return self._check(ctx, dry_run=False)
