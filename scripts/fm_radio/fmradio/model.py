"""What a candidate pressing is, and what we actually know about one.

Plain dataclasses with no MusicBrainz or ListenBrainz types in them. The
scorer takes these and nothing else, so the heuristic can be tested
exhaustively offline and a third metadata source could be added later without
touching a scoring rule.

Every field is optional because both sources genuinely omit any of them. A
scorer that assumes a field is present is one that crashes on the first sparse
release, and sparse releases are commonest for exactly the 1950s-60s material
this library is heaviest in.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Markers meaning "this is not the pressing anyone heard on the radio".
#: Matched against title + disambiguation, lowercased.
#:
#: NOTE what is deliberately ABSENT: "edit". ORPHEUS's
#: orpheus_fm_radio_identifier.py listed it under exclude_keywords, which
#: filtered out its own best signal -- a *radio edit* or *single edit* is
#: frequently the exact version wanted. It is a POSITIVE marker here.
DISQUALIFYING = (
    "karaoke",
    "tribute",
    "made popular by",
    "in the style of",
)

#: Markers that make a pressing unlikely but not impossible. Penalised rather
#: than excluded, because sometimes the remaster is the only copy that exists.
PENALTY_MARKERS: dict[str, int] = {
    "remaster": -30,
    "remastered": -30,
    "remix": -35,
    "demo": -40,
    "instrumental": -35,
    "acoustic": -25,
    "rehearsal": -40,
    "alternate take": -35,
    "alternate version": -30,
    "outtake": -40,
    "a cappella": -30,
    "extended": -20,
    "12\" version": -20,
    "dub": -25,
}

#: Markers that this IS the single as broadcast. The other half of ORPHEUS's
#: inverted rule.
SINGLE_MARKERS: dict[str, int] = {
    "single version": 25,
    "single edit": 25,
    "radio edit": 30,
    "radio version": 30,
    "7\" version": 20,
    "mono version": 10,
}


@dataclass(frozen=True)
class Candidate:
    """One pressing of one recording, as offered by a metadata source."""

    #: Recording or track title, e.g. "Bohemian Rhapsody (Single Edit)"
    title: str = ""
    #: MusicBrainz disambiguation comment, e.g. "2011 remaster"
    disambiguation: str = ""
    #: Name of the release this pressing appeared on
    release_title: str = ""
    #: release-group primary type: "Single", "Album", "EP", "Broadcast", ...
    release_group_type: str = ""
    #: release-group secondary types: "Compilation", "Live", "Remix", ...
    secondary_types: tuple[str, ...] = ()
    #: The RECORDING's own first release date -- "1975", "1975-10", "1975-10-31".
    #:
    #: Not the release group's. MetaBrainz's own docs warn that
    #: %originalyear% is the release group's first release date and NOT the
    #: recording's; a 1975 recording on a 1990 Greatest Hits release group has
    #: a release-group date of 1990. Using the wrong one ranks every
    #: compilation as a modern reissue. MUSAEUS's original_year.py already
    #: gets this right by taking the minimum of the two.
    first_release_date: str = ""
    #: Recording length in seconds. None when the source has no length.
    length_seconds: float | None = None
    #: "Vinyl", "CD", "Cassette", "Digital Media", ...
    media_format: str = ""
    #: ISO country of the release: "GB", "US", "XW" (worldwide)
    country: str = ""
    #: Total ListenBrainz listens. None means UNKNOWN, never zero.
    #:
    #: The distinction is load-bearing. ListenBrainz returns null for
    #: recordings it has no data for, and collapsing that to 0 would rank an
    #: unmeasured pressing identically to one nobody plays. That exact
    #: conflation is what made MUSAEUS's report say nothing at all about
    #: 2,862 pending duplicate groups.
    listen_count: int | None = None
    #: Unique ListenBrainz listeners. None means unknown, as above.
    listener_count: int | None = None
    #: Carried through so any proposal is checkable against the source.
    recording_mbid: str = ""
    release_mbid: str = ""

    @property
    def year(self) -> int | None:
        """Leading year of first_release_date, or None if unparseable.

        Dates arrive as 'YYYY', 'YYYY-MM', 'YYYY-MM-DD' and occasionally junk.
        Parsed once here rather than in every rule that wants a year.
        """
        head = (self.first_release_date or "").strip()[:4]
        return int(head) if head.isdigit() else None

    @property
    def haystack(self) -> str:
        """Lowercased text the marker rules search."""
        return f"{self.title} {self.disambiguation} {self.release_title}".lower()

    @property
    def is_live(self) -> bool:
        """Live takes are excluded outright: FM played the studio cut.

        Checked against secondary types AND text, because MusicBrainz carries
        it in either place depending on who entered the release.
        """
        if any(t.lower() == "live" for t in self.secondary_types):
            return True
        return "(live" in self.haystack or " live at " in self.haystack


@dataclass
class Verdict:
    """A score, and -- as importantly -- why.

    `reasons` exists because a heuristic that cannot explain itself is one a
    reviewer either trusts blindly or ignores entirely. Every rule that moves
    the score appends a line, so the CSV carries the argument and not just a
    number.
    """

    candidate: Candidate
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    #: Set when a rule excludes the candidate outright.
    excluded: str = ""

    def add(self, points: int, why: str) -> None:
        if points:
            self.score += points
            self.reasons.append(f"{points:+d} {why}")

    def note(self, why: str) -> None:
        """Record a fact that moved no points -- absence of data, usually."""
        self.reasons.append(f"  0 {why}")

    def exclude(self, why: str) -> None:
        self.excluded = why
        self.reasons.append(f"EXCLUDED {why}")

    @property
    def explanation(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "no signal either way"


@dataclass
class Proposal:
    """The tool's output for one song: a ranking, and how sure it is.

    `confident` is deliberately conservative. This proposes into a CSV and
    never decides -- every other review artefact in this project has the same
    shape -- so a narrow margin is reported as needing a human rather than
    resolved by rounding.
    """

    artist: str
    title: str
    verdicts: list[Verdict]
    #: Points between the top candidate and the runner-up. None if <2 survive.
    margin: int | None = None
    #: Why no confident answer, when there isn't one.
    undecided: str = ""

    @property
    def best(self) -> Verdict | None:
        return self.verdicts[0] if self.verdicts else None

    @property
    def survivors(self) -> int:
        return sum(1 for v in self.verdicts if not v.excluded)

    @property
    def chose(self) -> bool:
        """Did a ranking actually happen? One candidate is not a choice.

        Added 2026-09-10 after the first live run. Four of seven "CONFIDENT"
        rows had a blank margin, meaning exactly one candidate survived -- so
        the tool had reported confidence in a decision it never made. That is
        the same shape of error as a green check over an empty measurement, and
        it was mine.
        """
        return self.survivors > 1

    @property
    def judged_originality(self) -> bool:
        """Did any surviving candidate carry a date to reason about?

        ListenBrainz returns no first-release-date and no release-group type,
        so a pass-1-only verdict can score popularity and length and nothing
        else -- while "which pressing came first" is the signal this whole tool
        exists for. A verdict without it is not wrong, but it is not an answer
        to the question asked, and the report must say so.
        """
        return any(
            v.candidate.year is not None for v in self.verdicts if not v.excluded
        )

    @property
    def confident(self) -> bool:
        return not self.undecided and self.chose and self.judged_originality
