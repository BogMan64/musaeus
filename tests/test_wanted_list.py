"""Turning a knock-off's own title into a TuneMyMusic wanted-list line.

Built by hand once (2026-09-02, MUSAEUS_WANTED_LIST.txt) after Grey asked
for it verbally; automated the next day after he asked for it to happen
without being asked again. The credit patterns and the "already owned"
prefix-match length are the exact ones measured against the live library
that day -- not re-derived here, reused, because a fresh attempt at the
same extraction logic is exactly the shape this project keeps finding
duplicated (see CLAUDE.md).
"""

from __future__ import annotations

import sqlite3

import pytest

from musaeus.wanted_list import (
    already_owned,
    clean_title,
    extract_credited_artist,
    wanted_lines,
)


class TestExtractCreditedArtist:
    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Midnight Cruiser [In the Style of Steely Dan]", "Steely Dan"),
            ("The South's Gonna Do It Again (Originally Performed By the Charlie "
             "Daniels Band)", "the Charlie Daniels Band"),
            ("Don't You Want Me (As Made Famous by The Human League)", "The Human League"),
            ("Nuthin' But a G Thang - Sound-a-Like As Made Famous By - Dr. Dre",
             "Dr. Dre"),
        ],
    )
    def test_the_three_proven_patterns(self, title, expected):
        assert extract_credited_artist(title) == expected

    @pytest.mark.parametrize(
        "title",
        [
            "Just a Regular Song",
            "Song (Live)",
            "Song [2015 Remaster]",
            "",
        ],
    )
    def test_no_credit_is_none_not_a_guess(self, title):
        assert extract_credited_artist(title) is None

    def test_none_title_does_not_crash(self):
        assert extract_credited_artist(None) is None


class TestCleanTitle:
    def test_bracketed_credit_removed(self):
        assert clean_title("Midnight Cruiser [In the Style of Steely Dan]") == "Midnight Cruiser"

    def test_dash_form_sound_a_like_removed(self):
        assert (
            clean_title("Nuthin' But a G Thang - Sound-a-Like As Made Famous By - Dr. Dre")
            == "Nuthin' But a G Thang"
        )

    def test_curly_brace_markup_removed(self):
        """The bracket-style gap CLAUDE.md documents -- {} not just ()/[]
        -- must not resurface here via a fresh hand-rolled strip. Routed
        through brackets.strip_bracketed for exactly this reason."""
        assert clean_title("Song {Karaoke Version}") == "Song"

    def test_plain_title_is_unchanged(self):
        assert clean_title("Hallelujah") == "Hallelujah"


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE archive (artist TEXT, title TEXT, status TEXT)")
    return c


class TestAlreadyOwned:
    def test_true_when_the_real_recording_is_catalogued(self, conn):
        conn.execute(
            "INSERT INTO archive (artist, title, status) VALUES (?,?,?)",
            ("Steely Dan", "Midnight Cruiser", "CATALOGUED"),
        )
        assert already_owned(conn, "Steely Dan", "Midnight Cruiser") is True

    def test_false_when_nothing_matches(self, conn):
        assert already_owned(conn, "Steely Dan", "Midnight Cruiser") is False

    def test_a_non_catalogued_row_does_not_count(self, conn):
        """A row sitting in DUPE_REVIEW or PENDING is not something Grey
        can currently play -- only a CATALOGUED row means "already have
        it"."""
        conn.execute(
            "INSERT INTO archive (artist, title, status) VALUES (?,?,?)",
            ("Steely Dan", "Midnight Cruiser", "PENDING"),
        )
        assert already_owned(conn, "Steely Dan", "Midnight Cruiser") is False

    def test_empty_artist_or_title_is_false_not_a_wildcard_match(self, conn):
        conn.execute(
            "INSERT INTO archive (artist, title, status) VALUES (?,?,?)",
            ("Anyone", "Anything", "CATALOGUED"),
        )
        assert already_owned(conn, "", "Anything") is False
        assert already_owned(conn, "Anyone", "") is False


class TestWantedLines:
    def test_a_credited_unowned_track_produces_a_line(self, conn):
        items = [{"title": "Midnight Cruiser [In the Style of Steely Dan]"}]
        assert wanted_lines(conn, items) == ["Steely Dan - Midnight Cruiser"]

    def test_an_already_owned_credited_track_produces_nothing(self, conn):
        """The whole point of checking ownership at all -- see the module
        docstring: 6 of 18 credited knock-offs measured 2026-09-02 were
        already owned, and a naive export would have re-acquired them."""
        conn.execute(
            "INSERT INTO archive (artist, title, status) VALUES (?,?,?)",
            ("Steely Dan", "Midnight Cruiser", "CATALOGUED"),
        )
        items = [{"title": "Midnight Cruiser [In the Style of Steely Dan]"}]
        assert wanted_lines(conn, items) == []

    def test_an_uncredited_track_is_silently_skipped(self, conn):
        items = [{"title": "Nobody Anywhere Karaoke Version"}]
        assert wanted_lines(conn, items) == []

    def test_order_follows_input_order(self, conn):
        items = [
            {"title": "Song A [In the Style of Artist One]"},
            {"title": "Song B [In the Style of Artist Two]"},
        ]
        assert wanted_lines(conn, items) == [
            "Artist One - Song A",
            "Artist Two - Song B",
        ]

    def test_missing_title_key_does_not_crash(self, conn):
        assert wanted_lines(conn, [{}]) == []
