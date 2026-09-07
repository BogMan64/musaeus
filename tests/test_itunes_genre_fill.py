"""iTunes genre fill — the parts that must not go wrong.

Ported in concept from ORPHEUS, which MUSAEUS never had. The first live run
found both of the failures pinned here, so neither is hypothetical.

No test makes a network request.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from itunes_genre_fill import (  # noqa: E402
    SUSPICIOUS,
    fold,
    genre_map,
    library_genre,
    unknown_artists,
)


@pytest.fixture
def vault(tmp_path):
    meta = tmp_path / "MetaData"
    meta.mkdir()
    (meta / "MasterLaw.csv").write_text("Byrds, The,Rock\n", encoding="utf-8")
    (meta / "Genre_Allowed.txt").write_text("Rock\nJazz\nHoliday\n", encoding="utf-8")
    (meta / "Genre_Canonical_Map.txt").write_text(
        "# a comment\nAlternative/Indie => Alternative\nblues => Blues\n", encoding="utf-8")
    db = tmp_path / "musaeus.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE archive (artist TEXT, title TEXT, genre TEXT, status TEXT)")
    conn.commit()
    conn.close()
    return SimpleNamespace(db_path=db, meta_dir=meta, alac_archive=tmp_path)


def _add(vault, artist, genre="", status="CATALOGUED", title="Song"):
    conn = sqlite3.connect(vault.db_path)
    conn.execute("INSERT INTO archive VALUES (?,?,?,?)", (artist, title, genre, status))
    conn.commit()
    conn.close()


class TestTheLibraryIsAskedFirst:
    """The correctness fix, not an optimisation.

    The first live run wrote "Tiny Bradshaw,Pop" into MasterLaw while his one
    catalogued track had said Jazz all along -- two authorities disagreeing,
    created by the script meant to settle them.
    """

    def test_a_settled_artist_yields_the_librarys_own_genre(self, vault):
        _add(vault, "Tiny Bradshaw", "Jazz")
        _add(vault, "Tiny Bradshaw", "Jazz", title="Another")
        assert library_genre(vault, "Tiny Bradshaw") == "Jazz"

    def test_a_split_artist_yields_nothing(self, vault):
        """Two genres is not agreement, so iTunes still gets asked."""
        _add(vault, "Split", "Jazz")
        _add(vault, "Split", "Rock", title="Other")
        assert library_genre(vault, "Split") is None

    def test_a_blank_genre_is_not_agreement(self, vault):
        _add(vault, "Blank", "")
        assert library_genre(vault, "Blank") is None

    def test_only_catalogued_rows_count(self, vault):
        """A deleted row's genre is not evidence about the live library."""
        _add(vault, "Gone", "Jazz", status="DELETED")
        assert library_genre(vault, "Gone") is None


class TestSuspiciousGenres:
    """iTunes returned "Holiday" for a Blues Brothers covers act, and the
    high-confidence test wrote it, because "Holiday" IS in the vocabulary.
    Rare genres are where a wrong answer hides."""

    def test_holiday_is_suspicious(self):
        assert "Holiday" in SUSPICIOUS

    def test_the_common_genres_are_not(self):
        for g in ("Rock", "Jazz", "Blues", "Pop", "Classical", "Hip Hop"):
            assert g not in SUSPICIOUS


class TestUnknownArtists:
    def test_an_artist_in_the_law_is_not_unknown(self, vault):
        _add(vault, "The Byrds", "Rock")
        assert unknown_artists(vault) == []

    def test_the_article_convention_folds(self, vault):
        """"Byrds, The" is in the law; "The Byrds" must not read as unknown.
        That mismatch left 246 MasterLaw rules dormant once already."""
        assert fold("The Byrds") == fold("Byrds, The") == fold("Byrds (the)")

    def test_an_unknown_artist_is_listed_with_its_track_count(self, vault):
        _add(vault, "Nobody", "Rock")
        _add(vault, "Nobody", "Rock", title="Two")
        assert unknown_artists(vault) == [("Nobody", 2)]

    def test_results_are_ordered_by_track_count(self, vault):
        _add(vault, "Small", "Rock")
        for i in range(3):
            _add(vault, "Big", "Rock", title="t%d" % i)
        assert [a for a, _ in unknown_artists(vault)] == ["Big", "Small"]


class TestGenreMap:
    def test_comments_and_case_are_handled(self, vault):
        m = genre_map(vault)
        assert m["alternative/indie"] == "Alternative"
        assert m["blues"] == "Blues"
        assert not any(k.startswith("#") for k in m)

    def test_a_missing_map_is_not_an_error(self, vault):
        (vault.meta_dir / "Genre_Canonical_Map.txt").unlink()
        assert genre_map(vault) == {}
