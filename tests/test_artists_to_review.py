"""ArtistsToReview.csv — the queue for artists no authority has ruled on.

Grey, 2026-09-07: "This way the enduser can look at new artist if any ever
are spotted and can decide the next move." An artist nobody has ruled on is
the shape every knock-off arrives in.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from artists_to_review import collect, fold, known_names  # noqa: E402


@pytest.fixture
def vault(tmp_path):
    meta = tmp_path / "MetaData"
    meta.mkdir()
    (meta / "MasterLaw.csv").write_text("Byrds, The,Rock\nLizzo,Pop\n", encoding="utf-8")
    (meta / "artist_canon.tsv").write_text(
        "# comment\nDanny\tDanny & The Juniors\n", encoding="utf-8"
    )
    db = tmp_path / "musaeus.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE archive (artist TEXT, title TEXT, status TEXT)")
    conn.commit()
    conn.close()
    return SimpleNamespace(db_path=db, meta_dir=meta, alac_archive=tmp_path)


def _add(vault, artist, title="Song", status="CATALOGUED"):
    conn = sqlite3.connect(vault.db_path)
    conn.execute("INSERT INTO archive VALUES (?,?,?)", (artist, title, status))
    conn.commit()
    conn.close()


class TestKnownNames:
    def test_the_article_convention_folds_both_ways(self, vault):
        """The whole check turns on this. "Byrds, The" is in the law; an
        incoming "The Byrds" must not read as unknown. That exact mismatch
        left 246 MasterLaw rules dormant once before."""
        known = known_names(vault)
        assert fold("The Byrds") in known
        assert fold("Byrds (the)") in known

    def test_a_rename_TARGET_counts_as_known(self, vault):
        """artist_canon maps raw -> canonical. A name that only ever appears
        as the target is still an artist someone has ruled on, so both sides
        of the mapping are read."""
        known = known_names(vault)
        assert fold("Danny & The Juniors") in known
        assert fold("Danny") in known


class TestCollect:
    def test_an_unknown_artist_is_listed(self, vault):
        _add(vault, "Party Tyme", "I Gotta Right To Sing The Blues")
        rows = collect(vault)
        assert [r["artist"] for r in rows] == ["Party Tyme"]
        assert rows[0]["tracks"] == 1
        assert rows[0]["example_track"] == "I Gotta Right To Sing The Blues"

    def test_a_known_artist_is_not_listed(self, vault):
        _add(vault, "Lizzo")
        _add(vault, "The Byrds")
        assert collect(vault) == []

    def test_only_catalogued_rows_count(self, vault):
        """A deleted knock-off has already been ruled on."""
        _add(vault, "Party Tyme", status="DELETED")
        _add(vault, "Studio 99", status="GHOST")
        assert collect(vault) == []

    def test_rows_are_ordered_by_track_count(self, vault):
        for _ in range(3):
            _add(vault, "Party Tyme")
        _add(vault, "Studio 99")
        assert [r["artist"] for r in collect(vault)] == ["Party Tyme", "Studio 99"]

    def test_the_decision_column_names_all_three_moves(self, vault):
        """Not just RENAME. Naming the file after the canon would steer
        every ruling towards a rename; REMOVE and KEEP are equally likely."""
        _add(vault, "Party Tyme")
        col = list(collect(vault)[0].keys())[0]
        assert "KEEP" in col and "REMOVE" in col and "RENAME" in col

    def test_a_blank_artist_is_not_an_unknown_artist(self, vault):
        _add(vault, "")
        assert collect(vault) == []
