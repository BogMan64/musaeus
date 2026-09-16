"""Rank the pressings of one song. Pure: no network, no clock, no I/O.

WHY A SET AND NOT ONE CANDIDATE
Several of the strongest signals are only meaningful relative to the other
pressings of the same song. "Earliest" needs the others to compare against;
so does "most played" and "the short one". A score(candidate) function cannot
express them, so this is rank(candidates) -> ordered verdicts.

WHAT "THE FM RADIO VERSION" ACTUALLY MEANS HERE
Nobody can measure it directly. MusicBrainz holds no chart data at all, and
the obvious substitute is a trap: billboard-charts' own PyPI page warns that
Billboard returns HTTP 200 with plausible markup for weeks that never
existed, stamped with whatever date you asked for -- so you store fabricated
history that looks complete.

So this approximates from three independent directions, and says which of
them it had:

  1. WAS IT A SINGLE       release-group type, plus single/radio-edit markers
  2. WAS IT FIRST          the recording's own first-release-date
  3. IS IT THE ONE PLAYED  ListenBrainz total listens

Any one alone is weak. The 2011 remaster of a 1975 single is a Single with a
recent date; a 1975 album cut is early but was never on the radio; the most
played version of a Beatles song may be a 2009 remaster because that is what
streaming carries. Agreement between two or three is the signal; disagreement
is reported as ambiguity rather than resolved by the largest number.

GREY'S RULING, 2026-09-10, on which way that conflict resolves:

    "I prefer the original version as it is what my ears remember."

So when play count and first-release-date disagree -- which for catalogue
artists is the normal case, because streaming carries the remaster -- the
ORIGINAL wins. That is a decision, not a heuristic: W_MOST_PLAYED (25) is
deliberately smaller than the remaster penalty (30) plus W_EARLIEST (30), so
listens can corroborate an original and can break a tie between two originals,
but cannot on their own promote a remaster above the pressing it was made
from. Do not "fix" that asymmetry by raising W_MOST_PLAYED; it is the ruling.
"""

from __future__ import annotations

from .model import (
    DISQUALIFYING,
    PENALTY_MARKERS,
    SINGLE_MARKERS,
    Candidate,
    Proposal,
    Verdict,
)

#: A single as broadcast. Lower bound excludes jingles and intro tracks; upper
#: bound is where the album cut usually lives.
#:
#: 100s not 120s deliberately. ORPHEUS used (120, 360), which would have ruled
#: out the early rock this library is full of -- section 5 measured a 120-second
#: floor flagging 397 complete recordings, including "Hit the Road Jack" at
#: 2:00 and "All Shook Up" at 1:58. 1:40 clears them.
SINGLE_LENGTH = (100.0, 330.0)

#: Weights. Kept as named constants rather than inline numbers so the ranking
#: can be re-tuned in one place and a test can assert a specific rule fired.
W_TYPE_SINGLE = 40
W_TYPE_EP = 10
W_EARLIEST = 30
W_EARLY_TIE = 20
W_MOST_PLAYED = 25
W_SECOND_PLAYED = 10
W_SINGLE_LENGTH = 15
W_SHORTEST_WHEN_LONG_SIBLING = 20
W_COMPILATION = -5
W_LONG_CUT = -20
W_PERIOD_FORMAT = 5

#: Below this the top two are too close to call and it goes to a human.
CONFIDENT_MARGIN = 15


#: An instrumental with a vocal sibling is a B-side; an instrumental with no
#: vocal counterpart is very likely the hit itself.
#:
#: This started as a flat -35 and the first test caught it: Jan & Dean's
#: instrumental won on "Single" plus "earliest" despite the penalty. Making the
#: penalty merely larger would have been the wrong fix, because instrumentals
#: genuinely were radio hits -- "Green Onions", "Telstar", "Apache", and
#: Grey's own MasterLaw keeps "Mort Stevens & His Orchestra, Hawaii Five-O"
#: under Surf Rock. Excluding them outright would lose real singles.
#:
#: So the rule is relative, like "earliest" and "most played": the penalty
#: applies only when a NON-instrumental pressing of the same song exists to
#: contrast with, and then it is heavy enough to overcome type and date.
W_INSTRUMENTAL_WITH_VOCAL_SIBLING = -70
W_INSTRUMENTAL_ALONE = 0

_VARIANT_MARKERS = ("instrumental", "a cappella", "karaoke")


def _is_variant(c: Candidate) -> bool:
    return any(m in c.haystack for m in _VARIANT_MARKERS)


def _mark(v: Verdict, c: Candidate, *, has_plain_sibling: bool) -> None:
    """Text markers: the single/radio signal, and the penalties."""
    hay = c.haystack
    for marker, points in SINGLE_MARKERS.items():
        if marker in hay:
            v.add(points, f"titled {marker!r}")
    for marker, points in PENALTY_MARKERS.items():
        if marker == "instrumental":
            continue  # handled relatively, below
        if marker in hay:
            v.add(points, f"titled {marker!r}")

    if "instrumental" in hay:
        if has_plain_sibling:
            v.add(
                W_INSTRUMENTAL_WITH_VOCAL_SIBLING,
                "instrumental, and a vocal pressing of this song exists",
            )
        else:
            v.note("instrumental, but no vocal pressing here -- may be the hit itself")


def rank(artist: str, title: str, candidates: list[Candidate]) -> Proposal:
    """Order *candidates* best-first and say how confident the top pick is."""
    if not candidates:
        return Proposal(artist, title, [], undecided="no candidates from any source")

    verdicts = [Verdict(candidate=c) for c in candidates]

    # ── Exclusions first, so relative rules compare only real contenders.
    # A live take that survived into "earliest" would drag the whole ranking.
    for v in verdicts:
        c = v.candidate
        if c.is_live:
            v.exclude("live recording -- FM played the studio cut")
            continue
        hit = next((d for d in DISQUALIFYING if d in c.haystack), None)
        if hit:
            v.exclude(f"{hit!r} -- not the artist's own recording")

    live = [v for v in verdicts if not v.excluded]
    if not live:
        return Proposal(
            artist, title, verdicts, undecided="every candidate was excluded"
        )

    # ── Set-relative facts, computed once over the survivors.
    years = [v.candidate.year for v in live if v.candidate.year is not None]
    earliest = min(years) if years else None

    plays = [
        v.candidate.listen_count for v in live if v.candidate.listen_count is not None
    ]
    most_played = max(plays) if plays else None
    # Second distinct count, so a near-tie is not rewarded as a clear win.
    distinct_plays = sorted(set(plays), reverse=True)
    runner_up_plays = distinct_plays[1] if len(distinct_plays) > 1 else None

    lengths = [
        v.candidate.length_seconds
        for v in live
        if v.candidate.length_seconds is not None
    ]
    shortest = min(lengths) if lengths else None
    longest = max(lengths) if lengths else None

    # Is there a pressing of this song that is not an instrumental/a cappella
    # variant? Decides whether the instrumental penalty applies at all.
    has_plain_sibling = any(not _is_variant(v.candidate) for v in live)

    for v in live:
        c = v.candidate

        # 1. WAS IT A SINGLE
        rgt = (c.release_group_type or "").lower()
        if rgt == "single":
            v.add(W_TYPE_SINGLE, "released as a Single")
        elif rgt == "ep":
            v.add(W_TYPE_EP, "released on an EP")
        elif not rgt:
            v.note("no release-group type from the source")
        if any(t.lower() == "compilation" for t in c.secondary_types):
            v.add(W_COMPILATION, "compilation release")

        _mark(v, c, has_plain_sibling=has_plain_sibling)

        # 2. WAS IT FIRST
        if c.year is None:
            v.note("no first-release-date -- cannot judge whether it came first")
        elif earliest is not None and c.year == earliest:
            v.add(W_EARLIEST, f"earliest pressing here ({c.year})")
        elif earliest is not None and c.year <= earliest + 1:
            # Within a year: pressings often straddle a new year, and MB dates
            # are frequently year-only, so an exact match is a coarse test.
            v.add(W_EARLY_TIE, f"within a year of the earliest ({c.year})")

        # 3. IS IT THE ONE PLAYED
        if c.listen_count is None:
            v.note("no ListenBrainz data -- unknown, not zero")
        elif most_played is not None and c.listen_count == most_played:
            v.add(W_MOST_PLAYED, f"most played here ({c.listen_count:,} listens)")
        elif runner_up_plays is not None and c.listen_count == runner_up_plays:
            v.add(W_SECOND_PLAYED, f"second most played ({c.listen_count:,})")

        # Length, absolute and relative.
        if c.length_seconds is None:
            v.note("no length from the source")
        else:
            lo, hi = SINGLE_LENGTH
            if lo <= c.length_seconds <= hi:
                v.add(W_SINGLE_LENGTH, f"single length ({c.length_seconds / 60:.1f} min)")
            elif c.length_seconds > hi:
                v.add(W_LONG_CUT, f"longer than a single ({c.length_seconds / 60:.1f} min)")
            # The album-cut-versus-single-edit case: only meaningful when a
            # materially longer sibling exists to contrast with.
            if (
                shortest is not None
                and longest is not None
                and c.length_seconds == shortest
                and longest - shortest >= 45
            ):
                v.add(
                    W_SHORTEST_WHEN_LONG_SIBLING,
                    f"shortest here, {longest - shortest:.0f}s under the longest",
                )

        # Era-appropriate medium. Small: it is corroboration, not evidence.
        fmt = (c.media_format or "").lower()
        if c.year is not None and c.year < 1990 and ("vinyl" in fmt or '7"' in fmt):
            v.add(W_PERIOD_FORMAT, "period-appropriate pressing (vinyl)")

    live.sort(key=lambda v: -v.score)
    ordered = live + [v for v in verdicts if v.excluded]

    # ── Confidence. Deliberately conservative: this proposes into a CSV and
    # never decides, so a narrow margin is reported rather than rounded away.
    margin: int | None = None
    undecided = ""
    if len(live) == 1:
        margin = None
        if not live[0].reasons:
            undecided = "only one candidate and no signal about it"
    else:
        margin = live[0].score - live[1].score
        if margin < CONFIDENT_MARGIN:
            undecided = (
                f"top two are {margin} point(s) apart -- too close to call "
                f"({live[0].candidate.title!r} vs {live[1].candidate.title!r})"
            )

    return Proposal(artist, title, ordered, margin=margin, undecided=undecided)
