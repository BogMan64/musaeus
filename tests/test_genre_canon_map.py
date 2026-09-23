"""
Tests for GenreCanon's map loading.

The bug these exist for: the loader split map lines on TAB only, while
the real Genre_Canonical_Map.txt in the vault has used " => " since it
was written. Not one of its 51 rules ever loaded. Paired with a
Genre_Allowed.txt that did not exist at all, resolve() returned None for
every genre it was ever given -- so GenreCanon was wired into
EnrichStage and silently doing nothing. Found 2026-08-21.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.canon.genre import GenreCanon


@pytest.fixture
def canon(tmp_path: Path) -> GenreCanon:
    allowed = tmp_path / "Genre_Allowed.txt"
    allowed.write_text("# comment\nRock\nR&B/Funk/Soul\nPop, Rock\n", encoding="utf-8")
    mapping = tmp_path / "Genre_Canonical_Map.txt"
    mapping.write_text(
        "# comment line\n"
        "soul => R&B/Funk/Soul\n"
        "pop rock => Pop, Rock\n"
        "classic rock\tRock\n"  # legacy tab form must still work
        "\n"
        "malformed line with no separator\n",
        encoding="utf-8",
    )
    return GenreCanon(allowed, mapping)


def test_arrow_separated_rules_load(canon):
    """The form the real vault file actually uses."""
    assert canon.resolve("soul") == "R&B/Funk/Soul"


def test_arrow_rule_target_may_contain_commas(canon):
    """ "Pop, Rock" is a single genre whose name contains a comma."""
    assert canon.resolve("pop rock") == "Pop, Rock"


def test_legacy_tab_form_still_loads(canon):
    """Changing the parser must not break any file already using tabs."""
    assert canon.resolve("classic rock") == "Rock"


def test_malformed_lines_are_skipped_not_fatal(canon):
    assert canon.resolve("Rock") == "Rock"


def test_exact_allowed_match_is_case_insensitive(canon):
    assert canon.resolve("rock") == "Rock"


def test_unknown_genre_resolves_to_none(canon):
    # Nonsense must not fuzzy-match its way into a real genre.
    assert canon.resolve("zzzzzqqqq") is None


def test_missing_files_are_not_an_error(tmp_path):
    gc = GenreCanon(tmp_path / "nope.txt", tmp_path / "also-nope.txt")
    assert gc.resolve("Rock") is None
