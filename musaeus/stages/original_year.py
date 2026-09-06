#!/usr/bin/env python3
"""
MUSAEUS — Original Year Stage

Recovers the **recording's** first release year from MusicBrainz and stores
it beside the year the file already carries.

Why this exists
---------------
`year` in this library is the year of the *edition we hold*, not the year
the recording came out. Measured 2026-08-23 against the live vault:

    Rock & Roll   221 of 384 tracks dated >= 2010
    Blues         284 of 559 tracks dated >= 2010
    Beach Boys "409" (1962)            -> year = 2012
    Ad Libs "The Boy From New York City" (1964) -> year = 2022

The field was 99.9% populated the whole time, which is exactly why nothing
caught it: coverage was measured, meaning was not. Anything that reasons
about era from `year` — era playlists, a Classic/Modern genre split, a
"sort by age" view — inherits the reissue date and quietly gets it wrong.

What it does NOT do
-------------------
It never writes `year`. The edition year is real data about the file we
hold and is worth keeping; the recording year is a second, different fact.
Consumers should read `COALESCE(original_year, year)`. Overwriting would
also make the stage unrepeatable — after one pass there would be no way to
tell a corrected row from an uncorrected one.

Matching discipline
-------------------
A wrong year is worse than no year, because it is indistinguishable from a
right one downstream. Four guards, all of which must pass:

  1. MB search score >= _RECORDING_SCORE.
  2. The credited artist must match ours after article folding.
  3. Track length must agree within _LENGTH_TOLERANCE_S when both are
     known — this is what separates a recording from a cover of it.
  4. The recovered year must be <= the year the file already claims (an
     original cannot post-date its own reissue) and >= _EARLIEST_YEAR.

A candidate failing any guard is skipped and counted, never written with a
lower confidence. Rows that were checked and yielded nothing are stamped
with `original_year_checked_at` so a re-run does not pay for them again.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

from ..brackets import CLOSE, OPEN, strip_bracketed
from ..context import RunContext, StageResult
from ..db import ensure_columns
from .base import BaseStage, NO_VERIFICATION
from .enrich import _clean_artist_for_lookup
from .mb_enrich import _mb_get, _same_artist

logger = logging.getLogger(__name__)

_RECORDING_SCORE = 90  # minimum MB search score to consider a recording
_LENGTH_TOLERANCE_S = 8.0  # remasters drift a little; covers drift a lot
_EARLIEST_YEAR = 1900
_COMMIT_EVERY = 25
_CANDIDATE_LIMIT = 100  # MusicBrainz's page maximum -- see find_original_year

#: Pacing between requests. Deliberately slower than mb_enrich's 1.1 s.
#: MusicBrainz weights its rate limit by response size, and this stage asks
#: for 100 recordings per query where mb_enrich asks for 3. Measured
#: 2026-08-24: at 1.1 s the pass drew a 503 every few tracks, and each one
#: cost a 5 s backoff -- an effective 4-6 s per track. Paying 1.6 s up front
#: is faster than paying 5 s intermittently.
_PACING_S = 1.6

#: Prefix marking a miss caused by the network rather than by the data.
#: These must NOT be stamped as checked: a 503 says nothing about whether
#: the recording exists, and stamping it gives up on the row permanently.
#: Measured 2026-08-24 on the Pop pass -- 39 of 123 misses were transient,
#: and all 39 would have been abandoned by a re-run.
_TRANSIENT = "lookup error:"


def is_transient(reason: str) -> bool:
    """True when a miss was the network's fault and is worth retrying."""
    return reason.startswith(_TRANSIENT)

# Parenthetical/bracketed markers that describe the *edition*, not the
# recording. Stripped before searching so "California Girls (Stereo)" and
# "409 (Remastered 2012)" reach MusicBrainz as the titles it indexes.
# "Live", "Acoustic", "Demo" and "Radio Edit" are deliberately absent —
# those name a genuinely different recording with its own release date.
_EDITION_MARKER_RE = re.compile(
    # Bracket characters come from brackets.py, not hardcoded, so this
    # cannot drift from the others -- the earlier version hardcoded a
    # parenthesis-and-square-bracket class only, the same blind spot as
    # the three regexes fixed 2026-09-02 (missing the curly-brace form).
    # Quantifiers are escaped as {{4}} rather
    # than {4}: interpolating OPEN/CLOSE turns this into an f-string, and
    # an f-string silently eats an un-escaped \d{4} into \d4 -- hit for
    # real fixing neardupe.py's equivalent regexes the same day.
    rf"""\s*[{OPEN}]\s*
        (?:\d{{4}}\s+)?
        (?:digitally\s+)?
        (?:remaster(?:ed)?|re-?master(?:ed)?|mono|stereo|mono\s+version|
           stereo\s+version|album\s+version|single\s+version|
           \d+(?:st|nd|rd|th)\s+anniversary(?:\s+edition)?|
           deluxe(?:\s+edition)?|reissue|expanded(?:\s+edition)?)
        (?:\s+\d{{4}})?
        \s*[{CLOSE}]""",
    re.IGNORECASE | re.VERBOSE,
)

# Any bracketed suffix at all -- the fallback when the targeted edition
# markers above leave a title MusicBrainz still cannot find.


def strip_edition_markers(title: str) -> str:
    """ "409 (Remastered 2012)" -> "409".

    Only edition markers are removed. A title that is nothing but a marker
    is returned unchanged rather than emptied — searching MusicBrainz for
    "" matches everything, which is the worst possible failure here.
    """
    cleaned = _EDITION_MARKER_RE.sub("", title or "").strip()
    return cleaned or (title or "").strip()


def _year_of(date_str: str | None) -> int | None:
    if not date_str:
        return None
    head = date_str.strip()[:4]
    return int(head) if len(head) == 4 and head.isdigit() else None


def earliest_year(recording: dict) -> int | None:
    """Earliest release year evidenced by one MB recording result.

    Takes the minimum of the recording's own first-release-date and the
    dates of every release it appears on. MB populates these inconsistently
    — the recording-level field is absent on plenty of older entries, and
    the release list sometimes carries an earlier date than it — so the
    minimum of both is the only reading that does not silently prefer a
    reissue.
    """
    years = []
    y = _year_of(recording.get("first-release-date"))
    if y:
        years.append(y)
    for rel in recording.get("releases", []) or []:
        y = _year_of(rel.get("date"))
        if y:
            years.append(y)
        y = _year_of((rel.get("release-group") or {}).get("first-release-date"))
        if y:
            years.append(y)
    return min(years) if years else None


def _credited_artists(recording: dict) -> list[str]:
    out = []
    for credit in recording.get("artist-credit", []) or []:
        artist = credit.get("artist") or {}
        if artist.get("name"):
            out.append(artist["name"])
    return out


def find_original_year(
    artist: str,
    title: str,
    duration_s: float | None,
    known_year: int | None,
) -> tuple[int | None, str]:
    """Return (year, reason). `reason` explains a miss, for the report."""
    lookup_artist = _clean_artist_for_lookup((artist or "").strip())
    lookup_title = strip_edition_markers(title or "")
    if not lookup_artist or not lookup_title:
        return None, "no artist or title to search on"

    # NOT quote()d. The whole query is URL-encoded once by _mb_get's
    # urlencode(); pre-encoding here sends "The%20Beach%20Boys" as a literal
    # Lucene term and MusicBrainz matches nothing. Verified live 2026-08-23:
    # the pre-encoded form returned 0 results for a track the plain form
    # matched at score 100.
    attempts = [lookup_title]
    # Second chance for titles carrying a parenthetical MusicBrainz does not
    # index -- "Downtown (64 Original Release with Orchestra)" finds nothing
    # under its full title. Only tried when the first attempt misses, and only
    # when stripping actually leaves a title behind.
    bare = strip_bracketed(lookup_title)
    if bare and bare != lookup_title:
        attempts.append(bare)

    data: dict = {}
    for n, attempt in enumerate(attempts):
        # MusicBrainz allows 1 request/second for unauthenticated clients.
        # The caller sleeps once per track, which is enough for a single
        # lookup but not for a retry: the two requests went out back to back,
        # earning a 503 and a 5-second backoff that made the whole pass ~5x
        # slower than the rate limit it was trying to respect. Measured
        # 2026-08-24: ~6 s/track against a 1.1 s budget.
        if n:
            time.sleep(_PACING_S)
        query = f'recording:"{attempt}" AND artist:"{lookup_artist}"'
        try:
            data = _mb_get("recording", {"query": query, "limit": str(_CANDIDATE_LIMIT)})
        except Exception as exc:  # network/HTTP/parse — all non-fatal for one row
            logger.warning("[original-year] MB error for %r / %r: %s", artist, title, exc)
            return None, f"{_TRANSIENT} {exc}"
        if data.get("recordings"):
            break

    # Every candidate is weighed, not just the first acceptable one, and the
    # earliest surviving year wins. MusicBrainz orders by match score, not by
    # date, and reissues score identically to originals: for the Beach Boys'
    # "409" the 1962 recording came back 6th and 23rd out of 25, behind a 2022
    # and a 2011 pressing that scored the same 100.
    #
    # The page size is MB's maximum for the same reason. A heavily compiled
    # song has one recording entry per compilation: "Dancing Queen" has 102,
    # and at a page size of 25 every result was a reissue -- the stage
    # returned 1990 with full confidence. At 100 the 1976 original is in
    # range and it returns 1976. Verified live 2026-08-23, both values.
    best: int | None = None
    rejected = ""

    for rec in data.get("recordings", []) or []:
        if int(rec.get("score", 0)) < _RECORDING_SCORE:
            continue

        if artist and not any(_same_artist(artist, n) for n in _credited_artists(rec)):
            rejected = rejected or "artist credit did not match"
            continue

        mb_len = rec.get("length")
        if duration_s and mb_len and abs((mb_len / 1000.0) - duration_s) > _LENGTH_TOLERANCE_S:
            rejected = rejected or "track length disagreed"
            continue

        y = earliest_year(rec)
        if y is None:
            rejected = rejected or "no release date on the match"
            continue
        if y < _EARLIEST_YEAR:
            rejected = rejected or f"implausible year {y}"
            continue
        # An original cannot post-date the pressing we hold. When it does,
        # one of the two is wrong and we cannot tell which — so neither is
        # written.
        if known_year is not None and y > known_year:
            rejected = rejected or f"MB year {y} is later than the file's {known_year}"
            continue

        best = y if best is None else min(best, y)

    if best is None:
        return None, rejected or "no candidate scored high enough"
    return best, ""


def _ensure_columns(conn) -> None:  # type: ignore[type-arg]
    """Columns this stage owns. Mechanism shared via db.ensure_columns;
    the list stays here, next to the code that reads them."""
    ensure_columns(
        conn,
        (
            ("original_year", "TEXT"),
            ("original_year_source", "TEXT"),
            ("original_year_checked_at", "TEXT"),
        ),
    )
class OriginalYearStage(BaseStage):
    """
    Fill `original_year` from MusicBrainz for catalogued tracks.

    Never writes `year`. Reads nothing back that it wrote — verify_effect
    asserts against the edition year it promised not to touch.
    """

    NAME = "original-year"

    def validate(self, ctx: RunContext) -> None:
        _ensure_columns(ctx.conn)
        pending = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' "
            "AND original_year_checked_at IS NULL"
        ).fetchone()[0]
        logger.info("[original-year] %d catalogued track(s) not yet checked", pending)

    def _candidates(self, ctx: RunContext) -> list[dict]:
        limit = ctx.get("original_year_limit", 0) or 0
        genre = (ctx.get("original_year_genre", "") or "").strip()
        sql = (
            "SELECT id, file_path, artist, title, year, duration FROM archive "
            "WHERE status='CATALOGUED' AND original_year_checked_at IS NULL "
            "AND artist IS NOT NULL AND trim(artist) != '' "
            "AND title IS NOT NULL AND trim(title) != '' "
        )
        params: list[str] = []
        # A whole-library pass is hours of rate-limited lookups. Narrowing to
        # the genre a decision actually depends on lets that decision be made
        # tonight while the rest fills in behind it.
        if genre:
            sql += "AND genre = ? "
            params.append(genre)
        sql += "ORDER BY artist, title"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in ctx.conn.execute(sql, params).fetchall()]

    def _process(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        _ensure_columns(ctx.conn)

        rows = self._candidates(ctx)
        result.notes.append(f"candidates: {len(rows)}")
        if not rows:
            result.notes.append("nothing to do — every catalogued track has been checked")
            ctx.record_stage(result)
            return result

        # Recorded before the first write so the count survives a kill.
        self._baseline_years = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' "
            "AND year IS NOT NULL AND trim(year) != ''"
        ).fetchone()[0]

        found = corrected = missed = transient = 0
        reasons: dict[str, int] = {}

        for i, row in enumerate(rows, 1):
            result.files_processed += 1
            known = _year_of(row.get("year"))

            year, reason = find_original_year(
                row.get("artist") or "",
                row.get("title") or "",
                row.get("duration"),
                known,
            )
            time.sleep(_PACING_S)

            now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
            if year is None:
                missed += 1
                reasons[reason] = reasons.get(reason, 0) + 1
                # Only a decision about the *data* is recorded. A network
                # failure leaves the row untouched so a later pass retries it.
                if not dry_run and not is_transient(reason):
                    ctx.conn.execute(
                        "UPDATE archive SET original_year_checked_at=? WHERE id=?",
                        (now, row["id"]),
                    )
                else:
                    transient += 1
            else:
                found += 1
                if known is not None and year < known:
                    corrected += 1
                    logger.info(
                        "[original-year] %s — %s: file says %s, recording is %d",
                        row.get("artist"),
                        row.get("title"),
                        known,
                        year,
                    )
                if not dry_run:
                    ctx.conn.execute(
                        "UPDATE archive SET original_year=?, original_year_source=?, "
                        "original_year_checked_at=? WHERE id=?",
                        (str(year), "musicbrainz", now, row["id"]),
                    )
                    ctx.log_event(
                        "ORIGINAL_YEAR_SET",
                        file_path=row["file_path"],
                        old_value=row.get("year"),
                        new_value=str(year),
                        stage=self.NAME,
                    )
                result.files_changed += 1

            if not dry_run and i % _COMMIT_EVERY == 0:
                ctx.conn.commit()
                logger.info("[original-year] checkpoint %d/%d", i, len(rows))

        if not dry_run:
            ctx.conn.commit()

        verb = "would set" if dry_run else "set"
        result.notes.append(f"original year {verb}: {found}")
        result.notes.append(f"of those, earlier than the file's own year: {corrected}")
        result.notes.append(f"no confident match: {missed}")
        result.notes.append(f"    of those, transient — left unchecked for retry: {transient}")
        for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:5]:
            result.notes.append(f"    {n} × {reason}")

        ctx.record_stage(result)
        return result

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """Assert the promise this stage makes: it adds a fact, it destroys none.

        The post-condition that matters is about the column this stage does
        NOT write. A count of rows it did write would be satisfied by the
        stage re-reading itself; the edition year staying intact would not.
        """
        # The columns are created on first run (ensure_columns), so their
        # absence means this stage has not run here -- "I did not look",
        # not "I looked and it is broken". Without this the check raised
        # OperationalError against a library where original_year had never
        # been created, and _check_effect swallowed it into a warning: an
        # erroring check degrades silently to no check at all, which is the
        # failure this whole mechanism exists to prevent.
        cols = {r[1] for r in ctx.conn.execute("PRAGMA table_info(archive)")}
        if not {"original_year", "original_year_source",
                "original_year_checked_at"} <= cols:
            return NO_VERIFICATION

        problems: list[str] = []

        after = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' "
            "AND year IS NOT NULL AND trim(year) != ''"
        ).fetchone()[0]
        baseline = getattr(self, "_baseline_years", after)
        if after != baseline:
            problems.append(
                f"`year` population changed from {baseline} to {after} — this stage "
                "must never write the edition year"
            )

        # Nothing may claim a recording year later than the pressing it sits on.
        impossible = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' "
            "AND original_year IS NOT NULL AND year IS NOT NULL "
            "AND trim(year) != '' AND CAST(original_year AS INTEGER) > CAST(year AS INTEGER)"
        ).fetchone()[0]
        if impossible:
            problems.append(
                f"{impossible} row(s) carry an original_year later than their own year"
            )

        # A row that was written must carry both the value and its provenance;
        # a half-written row is the silent-no-op shape in a new costume.
        orphaned = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE original_year IS NOT NULL "
            "AND (original_year_source IS NULL OR original_year_checked_at IS NULL)"
        ).fetchone()[0]
        if orphaned:
            problems.append(f"{orphaned} row(s) have an original_year with no source/timestamp")

        return problems

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._process(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._process(ctx, dry_run=False)
