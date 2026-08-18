"""
Tests for musaeus.canon.artist — ArtistCanon.

Tests the TSV-backed artist name resolution (exact, fuzzy, add/persist).
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from musaeus.canon.artist import ArtistCanon


@pytest.fixture
def tsv_path(tmp_path: Path) -> Path:
    """Return a path for the TSV (not yet created)."""
    return tmp_path / "artist_canon.tsv"


@pytest.fixture
def canon(tsv_path: Path) -> ArtistCanon:
    """Create a canon with some initial entries."""
    tsv_path.write_text(
        "# Test canon\n"
        "the beatles\tThe Beatles\n"
        "portishead\tPortishead\n"
        "led zeppelin\tLed Zeppelin\n"
        "pink floyd\tPink Floyd\n",
        encoding="utf-8",
    )
    return ArtistCanon(tsv_path)


# ── Basic loading ─────────────────────────────────────────────────────────────

class TestArtistCanonLoad:
    def test_loads_entries(self, canon):
        assert len(canon) == 4

    def test_empty_file(self, tsv_path):
        tsv_path.write_text("", encoding="utf-8")
        ac = ArtistCanon(tsv_path)
        assert len(ac) == 0

    def test_missing_file(self, tmp_path):
        ac = ArtistCanon(tmp_path / "nonexistent.tsv")
        assert len(ac) == 0

    def test_comments_ignored(self, tsv_path):
        tsv_path.write_text(
            "# Comment\n"
            "# Another comment\n"
            "radiohead\tRadiohead\n",
            encoding="utf-8",
        )
        ac = ArtistCanon(tsv_path)
        assert len(ac) == 1


# ── Resolve (exact match) ────────────────────────────────────────────────────

class TestArtistCanonResolve:
    def test_exact_match_case_insensitive(self, canon):
        assert canon.resolve("THE BEATLES") == "The Beatles"
        assert canon.resolve("the beatles") == "The Beatles"
        assert canon.resolve("The Beatles") == "The Beatles"

    def test_resolve_with_extra_whitespace(self, canon):
        assert canon.resolve("  the   beatles  ") == "The Beatles"

    def test_no_match_returns_raw(self, canon):
        """If no match, returns the raw input unchanged."""
        assert canon.resolve("Unknown Artist") == "Unknown Artist"

    def test_empty_string(self, canon):
        assert canon.resolve("") == ""

    def test_has_method(self, canon):
        assert canon.has("the beatles") is True
        assert canon.has("THE BEATLES") is True
        assert canon.has("Unknown") is False


# ── Fuzzy resolution ──────────────────────────────────────────────────────────

class TestArtistCanonFuzzy:
    def test_fuzzy_match_close_spelling(self, canon):
        """With rapidfuzz available, close misspellings should resolve."""
        try:
            import rapidfuzz  # noqa: F401
            # "the betales" is close enough to "the beatles"
            result = canon.resolve("the betales")
            # Should fuzzy-match to The Beatles (score ~90+)
            assert result == "The Beatles"
        except ImportError:
            # If rapidfuzz not available, just returns raw
            result = canon.resolve("the betales")
            assert result == "the betales"

    @patch.dict("sys.modules", {"rapidfuzz": None, "rapidfuzz.process": None, "rapidfuzz.fuzz": None})
    def test_no_rapidfuzz_returns_raw(self, canon):
        """Without rapidfuzz, unmatched input returns as-is."""
        # This test verifies the ImportError path
        # Since rapidfuzz might be installed, we test the explicit no-match case
        result = canon.resolve("Completely Unknown Artist 12345")
        assert result == "Completely Unknown Artist 12345"


# ── Add and persist ───────────────────────────────────────────────────────────

class TestArtistCanonAdd:
    def test_add_new_entry(self, canon, tsv_path):
        canon.add("radiohead", "Radiohead")
        assert canon.resolve("radiohead") == "Radiohead"
        assert len(canon) == 5

        # Verify persisted to disk
        content = tsv_path.read_text(encoding="utf-8")
        assert "radiohead\tRadiohead" in content

    def test_add_with_whitespace(self, canon):
        canon.add("  Massive Attack  ", "Massive Attack")
        assert canon.resolve("massive attack") == "Massive Attack"

    def test_add_overwrites_existing(self, canon):
        canon.add("the beatles", "Beatles, The")
        assert canon.resolve("the beatles") == "Beatles, The"


# ── Utility methods ───────────────────────────────────────────────────────────

class TestArtistCanonUtility:
    def test_all_entries(self, canon):
        entries = canon.all_entries()
        assert len(entries) == 4
        # Sorted by normalised key
        keys = [e[0] for e in entries]
        assert keys == sorted(keys)

    def test_reload(self, canon, tsv_path):
        # Modify the file externally
        tsv_path.write_text("new artist\tNew Artist\n", encoding="utf-8")
        assert len(canon) == 4  # still has old data
        canon.reload()
        assert len(canon) == 1
        assert canon.resolve("new artist") == "New Artist"

    def test_repr(self, canon):
        r = repr(canon)
        assert "ArtistCanon" in r
        assert "entries=4" in r
