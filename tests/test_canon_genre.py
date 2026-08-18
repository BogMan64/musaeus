"""
Tests for musaeus.canon.genre — GenreCanon.

Tests the genre resolution system: allowed list, map, and fuzzy matching.
"""

from pathlib import Path

import pytest

from musaeus.canon.genre import GenreCanon


@pytest.fixture
def meta_dir(tmp_path: Path) -> Path:
    """Create a MetaData directory with genre files."""
    d = tmp_path / "MetaData"
    d.mkdir()
    return d


@pytest.fixture
def allowed_path(meta_dir: Path) -> Path:
    p = meta_dir / "genre_allowed.txt"
    p.write_text(
        "# Allowed genres\nRock\nElectronic\nHip-Hop\nJazz\nClassical\nMetal\nAmbient\nPop\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def map_path(meta_dir: Path) -> Path:
    p = meta_dir / "genre_map.tsv"
    p.write_text(
        "# Genre mappings\n"
        "hip-hop/rap\tHip-Hop\n"
        "electronica\tElectronic\n"
        "hard rock\tRock\n"
        "trip hop\tElectronic\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def gc(allowed_path: Path, map_path: Path) -> GenreCanon:
    return GenreCanon(allowed_path, map_path)


# ── Basic loading ─────────────────────────────────────────────────────────────


class TestGenreCanonLoad:
    def test_loads_allowed_list(self, gc):
        assert len(gc) == 8

    def test_all_allowed(self, gc):
        allowed = gc.all_allowed()
        assert "Rock" in allowed
        assert "Electronic" in allowed
        assert len(allowed) == 8

    def test_missing_files(self, tmp_path):
        gc = GenreCanon(
            tmp_path / "nonexistent_allowed.txt",
            tmp_path / "nonexistent_map.tsv",
        )
        assert len(gc) == 0

    def test_empty_files(self, meta_dir):
        a = meta_dir / "empty_allowed.txt"
        m = meta_dir / "empty_map.tsv"
        a.write_text("", encoding="utf-8")
        m.write_text("", encoding="utf-8")
        gc = GenreCanon(a, m)
        assert len(gc) == 0


# ── Resolve (exact match) ────────────────────────────────────────────────────


class TestGenreCanonResolve:
    def test_exact_match_case_insensitive(self, gc):
        assert gc.resolve("rock") == "Rock"
        assert gc.resolve("ROCK") == "Rock"
        assert gc.resolve("Rock") == "Rock"

    def test_exact_match_with_hyphen(self, gc):
        assert gc.resolve("Hip-Hop") == "Hip-Hop"
        assert gc.resolve("hip-hop") == "Hip-Hop"

    def test_empty_string_returns_none(self, gc):
        assert gc.resolve("") is None

    def test_none_like_empty(self, gc):
        # Passing empty means no genre
        assert gc.resolve("") is None


# ── Resolve (explicit map) ────────────────────────────────────────────────────


class TestGenreCanonMap:
    def test_map_lookup(self, gc):
        assert gc.resolve("hip-hop/rap") == "Hip-Hop"
        assert gc.resolve("electronica") == "Electronic"
        assert gc.resolve("hard rock") == "Rock"

    def test_map_case_insensitive(self, gc):
        assert gc.resolve("Hip-Hop/Rap") == "Hip-Hop"
        assert gc.resolve("ELECTRONICA") == "Electronic"


# ── Resolve (fuzzy match) ────────────────────────────────────────────────────


class TestGenreCanonFuzzy:
    def test_fuzzy_close_match(self, gc):
        """With rapidfuzz available, close genres should match."""
        try:
            import rapidfuzz  # noqa: F401

            # "Electroni" is very close to "Electronic"
            result = gc.resolve("Electroni")
            assert result == "Electronic"
        except ImportError:
            # Without rapidfuzz, no fuzzy match
            result = gc.resolve("Electroni")
            assert result is None

    def test_no_match_returns_none(self, gc):
        """Completely unrelated genre returns None."""
        result = gc.resolve("Completely Invented Genre XYZZY")
        assert result is None


# ── is_allowed / utility ──────────────────────────────────────────────────────


class TestGenreCanonUtility:
    def test_is_allowed_true(self, gc):
        assert gc.is_allowed("Rock") is True
        assert gc.is_allowed("Electronic") is True

    def test_is_allowed_false(self, gc):
        # is_allowed checks exact match (with strip)
        assert gc.is_allowed("Unknown") is False

    def test_is_allowed_case_sensitive(self, gc):
        # is_allowed uses `in self._allowed` which is case-sensitive
        assert gc.is_allowed("rock") is False  # "Rock" is allowed, not "rock"

    def test_reload(self, gc, allowed_path, map_path):
        # Modify allowed list
        allowed_path.write_text("NewGenre\n", encoding="utf-8")
        map_path.write_text("", encoding="utf-8")
        gc.reload()
        assert len(gc) == 1
        assert gc.resolve("newgenre") == "NewGenre"

    def test_repr(self, gc):
        r = repr(gc)
        assert "GenreCanon" in r
        assert "allowed=8" in r
        assert "mapped=4" in r
