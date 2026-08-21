"""
Tests for EnrichStage's article handling.

_clean_artist_for_lookup is the reverse of normalize.py's
_move_article_to_suffix: it converts MUSAEUS's canonical storage form
("Beatles, The") back to the natural form Last.fm expects ("The Beatles").
"""

from __future__ import annotations

import pytest

from musaeus.stages.enrich import _clean_artist_for_lookup


class TestArticleLookupDoubleForm:
    """_clean_artist_for_lookup consumed the parenthetical article but left a
    comma-article behind, yielding "The Beatles, The" -- a name Last.fm matches
    nothing against, so those artists silently never got enriched. Same bug
    class as the one fixed in normalize.py's forward transform."""

    def test_double_article_form_collapses(self):
        assert _clean_artist_for_lookup("Beatles, The (the)") == "The Beatles"

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            ("Beatles, The", "The Beatles"),
            ("Cranberries, The", "The Cranberries"),
            ("Archies (the)", "The Archies"),
            ("Tribe Called Quest, A", "A Tribe Called Quest"),
            ("Refused", "Refused"),
        ],
    )
    def test_ordinary_forms_unchanged(self, stored, expected):
        assert _clean_artist_for_lookup(stored) == expected

    @pytest.mark.parametrize("name", ["De La Soul", "Los Lobos", "La Roux", "Die Ärzte"])
    def test_protected_stylized_names_pass_through(self, name):
        assert _clean_artist_for_lookup(name) == name

    def test_regexes_are_shared_with_normalize_not_duplicated(self):
        # Duplicated article handling is what let this bug class regress three
        # separate times. Pin the single-definition invariant.
        from musaeus.stages import enrich, normalize

        assert enrich._ARTICLE_SUFFIX_RE is normalize._ARTICLE_SUFFIX_RE
        assert enrich._ARTICLE_COMMA_RE is normalize._ARTICLE_COMMA_RE
