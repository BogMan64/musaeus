"""The earliest-year preference must not silently unseat the source ruling.

Grey's ruling is a source ORDER: iTunes, MusicBrainz, Deezer, Last.fm,
Discogs. (MusicBrainz outranks Deezer -- worth stating, because the obvious
guess is wrong and a test that guesses it fails for the wrong reason.) The earliest-year preference was borrowed from beets to beat a
specific failure -- a compilation or reissue is later than the record a song
first appeared on, so the earliest dated answer is the better guess at the
original.

The first version implemented that by keeping ONLY the earliest-dated answers
and discarding the rest. That quietly did something else as well: an answer
with no year at all was dropped from the ballot, so iTunes lost to Deezer for
no reason except that iTunes had not said a year. The year is missing often
enough that this decided real rows, and the CSVs Grey reviewed on 2026-09-16
were generated under it.

The rule this file pins:

    dated and LATER than the earliest   -> demoted
    undated                             -> neither promoted nor demoted
    dated and earliest                  -> kept

"Not evidence of being early" is not the same as "evidence of being a
reissue", and only the second justifies removing an answer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "album_names"))

from propose_album_names import Answer, verdict  # noqa: E402


def A(album: str, year: str, source: str) -> Answer:
    return Answer(album=album, year=year, source=source)


class TestAnUndatedAnswerKeepsItsPlace:
    def test_the_top_source_is_not_unseated_for_omitting_a_year(self):
        """The regression, stated exactly. Three different albums, so this
        lands in the disagreement branch where the ordering runs."""
        _, album, _, win = verdict(
            A("iTunes Album", "", "itunes"),
            A("Deezer Album", "1970", "deezer"),
            A("MB Album", "1975", "musicbrainz"),
        )
        assert win.source == "itunes", f"source ruling inverted: {win.source} won"
        assert album == "iTunes Album"

    def test_with_no_years_anywhere_it_is_pure_source_order(self):
        _, _, _, win = verdict(
            A("I", "", "itunes"), A("D", "", "deezer"), A("M", "", "musicbrainz")
        )
        assert win.source == "itunes"

    def test_an_undated_lower_source_does_not_beat_a_dated_higher_one(self):
        """The mirror: neutral means neutral, not promoted."""
        _, _, _, win = verdict(
            A("iTunes Album", "1970", "itunes"),
            A("Deezer Album", "", "deezer"),
            A("MB Album", "1999", "musicbrainz"),
        )
        assert win.source == "itunes"


class TestTheEarliestYearStillWins:
    def test_a_later_dated_higher_source_loses_to_an_earlier_one(self):
        """The behaviour the rule exists for -- a 1995 reissue from the
        top-priority source must not beat the 1970 original."""
        _, _, _, win = verdict(
            A("iTunes Reissue", "1995", "itunes"),
            A("Deezer Original", "1970", "deezer"),
            A("MB Reissue", "1999", "musicbrainz"),
        )
        assert win.year == "1970"
        assert win.source == "deezer"

    def test_ties_among_the_earliest_fall_back_to_source_order(self):
        _, _, _, win = verdict(
            A("MB Album", "1970", "musicbrainz"),
            A("Deezer Album", "1970", "deezer"),
            A("LF Album", "1999", "lastfm"),
        )
        assert win.source == "musicbrainz"  # musicbrainz outranks deezer

    def test_demotion_never_empties_the_ballot(self):
        """If every answer is 'later' the filter must not leave nothing to
        pick from -- there is always exactly one earliest."""
        _, album, _, win = verdict(
            A("A", "1990", "itunes"), A("B", "1991", "deezer")
        )
        assert album in ("A", "B") and win is not None


class TestYearParsing:
    @pytest.mark.parametrize("junk", ["²", "19x0", "", "   ", "MCMLXX"])
    def test_a_year_int_would_reject_is_treated_as_undated_not_a_crash(self, junk):
        """`'²'.isdigit()` is True and `int('²')` raises, which is the
        CLAUDE.md 'compiles, imports, lints and lies' shape. .isdecimal() is
        the predicate that agrees with int()."""
        _, _, _, win = verdict(
            A("X", junk, "itunes"), A("Y", "1970", "deezer"), A("Z", "1980", "lastfm")
        )
        assert win is not None


class TestTheAgreementBarIsUntouched:
    def test_two_sources_agreeing_still_short_circuits(self):
        """None of this ordering should run when two sources agree -- that is
        the bar, and it is decided before any year is consulted."""
        tag, album, _, _ = verdict(
            A("Same Album", "1980", "deezer"), A("Same Album", "1999", "lastfm")
        )
        assert tag == "1-AGREED"
        assert album == "Same Album"
