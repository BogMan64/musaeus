"""Ask the library before asking the sources.

Measured 2026-09-15. Grey reviewed 42 tracks and produced verified album
names; 13 of them had already been filled from a sibling recording already in
his library. Where both had an answer, the library won five times out of six:

    Clapton, "Layla (Acoustic; Live at MTV Unplugged)"
        library -> "Unplugged"                     <- the actual record
        sources -> "Clapton Chronicles: The Best of Eric Clapton"

    Temptations, "All I Need (Edit)"
        library -> "With a Lot o' Soul"            <- the actual record
        sources -> "Gold"

    Beach Boys, "Shut Down (Stereo/Remastered 2003)"
        library -> "Surfin' USA"                   <- the actual record
        sources -> "The Very Best Of ... Sounds Of Summer"

The reason is structural, not luck: iTunes, Deezer and MusicBrainz rank by
search popularity, and a compilation outranks the original album it came
from. A shelf does not have that bias -- if the owner already holds the
record, that IS the answer.

So the library is consulted first, costs no request, and ranks above every
source in CONFIDENCE_ORDER.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "album_names"))

from propose_album_names import (  # noqa: E402
    CONFIDENCE_ORDER,
    ask_library,
    build_library_index,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE archive (artist TEXT, title TEXT, album TEXT, status TEXT)")
    c.executemany(
        "INSERT INTO archive (artist,title,album,status) VALUES (?,?,?,?)",
        [
            ("Eric Clapton", "Layla", "Unplugged", "CATALOGUED"),
            ("Temptations, The", "All I Need", "With a Lot o' Soul", "CATALOGUED"),
            ("Blur", "Song 2", "", "CATALOGUED"),                 # blank: not an answer
            ("Blur", "Beetlebum", "My playlist 3", "CATALOGUED"), # placeholder: not an answer
            ("Blur", "Coffee", "Unknown Album", "CATALOGUED"),    # placeholder: not an answer
            ("Gone", "Vanished", "Some Record", "QUARANTINED"),   # not catalogued
        ],
    )
    c.commit()
    return c


class TestItAnswersFromTheShelf:
    def test_a_filed_recording_is_found(self, conn):
        idx = build_library_index(conn)
        assert ask_library(idx, "Eric Clapton", "Layla").album == "Unplugged"

    def test_the_edition_suffix_does_not_prevent_a_match(self, conn):
        """song_key strips edition markers, which is the whole point --
        "Layla (Acoustic; Live at MTV Unplugged)" is the same recording."""
        idx = build_library_index(conn)
        got = ask_library(idx, "Eric Clapton", "Layla (Acoustic; Live at MTV Unplugged)")
        assert got.album == "Unplugged"

    def test_the_article_form_does_not_prevent_a_match(self, conn):
        idx = build_library_index(conn)
        assert ask_library(idx, "The Temptations", "All I Need (Edit)").album == (
            "With a Lot o' Soul"
        )

    def test_the_source_is_named_library(self, conn):
        idx = build_library_index(conn)
        assert ask_library(idx, "Eric Clapton", "Layla").source == "library"


class TestItRefusesToGuess:
    def test_a_recording_on_two_albums_answers_nothing(self):
        """The guard that matters.

        "The Power of Love" is on both the Back to the Future soundtrack and
        Greatest Hits. Picking one silently is how a track ends up filed
        under the wrong record -- and it would look exactly like a confident
        correct answer.
        """
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE archive (artist TEXT, title TEXT, album TEXT, status TEXT)")
        c.executemany(
            "INSERT INTO archive (artist,title,album,status) VALUES (?,?,?,?)",
            [
                ("Huey Lewis and the News", "The Power of Love",
                 "Back to the Future: Music From the Motion Picture Soundtrack", "CATALOGUED"),
                ("Huey Lewis and the News", "The Power of Love", "Greatest Hits", "CATALOGUED"),
            ],
        )
        c.commit()
        idx = build_library_index(c)
        assert ask_library(idx, "Huey Lewis and the News", "The Power of Love").album == ""

    @pytest.mark.parametrize("artist,title", [("Blur", "Song 2"), ("Blur", "Beetlebum"),
                                              ("Blur", "Coffee")])
    def test_a_blank_or_placeholder_album_is_not_an_answer(self, conn, artist, title):
        idx = build_library_index(conn)
        assert ask_library(idx, artist, title).album == ""

    def test_a_non_catalogued_row_is_not_consulted(self, conn):
        """QUARANTINED and GHOST rows are not the owner's filed shelf."""
        idx = build_library_index(conn)
        assert ask_library(idx, "Gone", "Vanished").album == ""

    def test_an_unknown_recording_answers_nothing(self, conn):
        idx = build_library_index(conn)
        assert ask_library(idx, "Nobody At All", "No Such Song").album == ""


class TestItOutranksEverySource:
    def test_the_library_tier_sorts_above_agreement(self):
        assert (CONFIDENCE_ORDER["0-ALREADY IN YOUR LIBRARY"]
                < CONFIDENCE_ORDER["1-AGREED"])

    def test_every_source_tier_ranks_below_it(self):
        top = CONFIDENCE_ORDER["0-ALREADY IN YOUR LIBRARY"]
        for tier, rank in CONFIDENCE_ORDER.items():
            if tier != "0-ALREADY IN YOUR LIBRARY":
                assert rank > top, f"{tier} must not outrank the owner's own shelf"
