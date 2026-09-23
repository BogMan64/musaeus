"""A studio album beats a compilation, and a fact beats a guess.

Two rules, and the second is the one that bit us.

**Studio album first, then earliest.** Ranking by year first hands you the
earliest COMPILATION, and a hits package is frequently older than the
remaster of the album it draws from.

**Real release metadata outranks the title heuristic.** COMPILATION_RE
guesses from words like "Greatest Hits" or "Vol. N". On 2026-09-14 it
condemned five real studio albums -- *The Traveling Wilburys, Vol. 1* and
*Vol. 3* are that band's own albums, and the pattern matches "Vol. N".
MusicBrainz says outright what a release is (primary-type Album with no
secondary type = studio album; any secondary type = Compilation / Live /
Soundtrack), so where a source tells us, we must not overrule it with a
guess.

Grey asked on 2026-09-14 that this be standard for every source rather than
a repair pass run after the fact.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "album_names"))

from propose_album_names import _choose, _is_compilation  # noqa: E402

# (track, collection, date, source[, is_compilation_from_metadata])
STUDIO = ("Handle With Care", "The Traveling Wilburys, Vol. 1", "1988", "musicbrainz", False)
HITS = ("Handle With Care", "Greatest Hits", "1985", "musicbrainz", True)


class TestMetadataOutranksTheGuess:
    def test_a_title_the_regex_hates_survives_when_metadata_says_album(self):
        """The Wilburys case, exactly."""
        assert _is_compilation(STUDIO) is False

    def test_metadata_can_also_condemn_an_innocent_looking_title(self):
        sneaky = ("t", "Pure Moods", "1994", "musicbrainz", True)
        assert _is_compilation(sneaky) is True

    def test_without_metadata_the_regex_is_consulted(self):
        assert _is_compilation(("t", "Greatest Hits", "1985", "itunes")) is True
        assert _is_compilation(("t", "Abbey Road", "1969", "itunes")) is False

    def test_an_explicit_unknown_falls_back_to_the_regex(self):
        """MusicBrainz sometimes returns a release with no type at all."""
        assert _is_compilation(("t", "Greatest Hits", "1985", "musicbrainz", None)) is True
        assert _is_compilation(("t", "Abbey Road", "1969", "musicbrainz", None)) is False


class TestRanking:
    def test_a_studio_album_beats_an_older_compilation(self):
        """The ordering bug: earliest-first alone picks the 1985 hits package."""
        got = _choose([HITS, STUDIO])
        assert got.album == "The Traveling Wilburys, Vol. 1"

    def test_among_studio_albums_the_earliest_still_wins(self):
        early = ("t", "Debut", "1970", "musicbrainz", False)
        later = ("t", "Reissue Deluxe", "1999", "musicbrainz", False)
        assert _choose([later, early]).album == "Debut"

    def test_among_compilations_the_earliest_still_wins(self):
        a = ("t", "Greatest Hits", "1990", "itunes", True)
        b = ("t", "Best Of", "1985", "itunes", True)
        assert _choose([a, b]).album == "Best Of"

    def test_a_four_tuple_candidate_still_works(self):
        """iTunes and Deezer supply no release type; they must not break."""
        got = _choose([("t", "Abbey Road", "1969", "itunes")])
        assert got.album == "Abbey Road"
        assert got.source == "itunes"

    def test_mixed_tuple_widths_rank_together(self):
        """One source knows the type, another does not -- both in one pool."""
        itunes_comp = ("t", "Ultimate Hits", "1975", "itunes")  # regex says compilation
        mb_studio = ("t", "Vol. 3", "1990", "musicbrainz", False)  # metadata says album
        assert _choose([itunes_comp, mb_studio]).album == "Vol. 3"


class TestItStillReportsHonestly:
    def test_the_chosen_year_and_source_come_from_the_winner(self):
        got = _choose([HITS, STUDIO])
        assert got.year == "1988"
        assert got.source == "musicbrainz"

    def test_no_candidates_is_an_empty_answer_not_a_crash(self):
        assert _choose([]).album == ""
