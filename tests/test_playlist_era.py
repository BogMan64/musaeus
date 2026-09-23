"""
Tests for PlaylistStage's per-decade era lists.

The era split was chosen over encoding eras as genre values: `year` is
populated on 99.9% of catalogued tracks, so a decade list is derivable and
re-cuttable without touching the canon. _decade is the whole contract --
if it ever guesses at a missing year, an Era_ list starts asserting a
release date the library does not actually hold.
"""

from __future__ import annotations

import pytest

from musaeus.stages.playlist import _decade


class TestDecadeMapping:
    @pytest.mark.parametrize(
        ("year", "expected"),
        [
            ("1967", "1960s"),
            ("1960", "1960s"),
            ("1969", "1960s"),
            ("1994", "1990s"),
            ("2000", "2000s"),
            ("2026", "2020s"),
            ("1949", "1940s"),
        ],
    )
    def test_years_map_to_their_decade(self, year, expected):
        assert _decade(year) == expected

    def test_boundary_year_belongs_to_the_later_decade(self):
        # 1999/2000 is the Classic/Modern cut the owner chose; the decade
        # label must not straddle it.
        assert _decade("1999") == "1990s"
        assert _decade("2000") == "2000s"


class TestUnknownYearsAreRefusedNotGuessed:
    """A missing year must drop the track from the era lists, not default it
    into one. Same shape as the silent-no-op family: a fabricated decade is
    indistinguishable from a real one once written to an M3U8."""

    @pytest.mark.parametrize("year", [None, "", "   ", "n/a", "19", "196", "abcd", "0"])
    def test_unusable_years_return_none(self, year):
        assert _decade(year) is None

    def test_timestamp_style_year_uses_only_the_year_part(self):
        # Some tags carry a full date; the decade comes from the year, and a
        # malformed remainder must not promote it to a guess.
        assert _decade("1973-04-01") == "1970s"
        assert _decade("197x-04-01") is None
