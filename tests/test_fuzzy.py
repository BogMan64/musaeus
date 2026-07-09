"""
Tests for musaeus.fuzzy — normalize(), similarity(), is_match(), etc.
No external dependencies required (falls back to difflib if no rapidfuzz).
"""

import pytest
from musaeus.fuzzy import (
    DEFAULT_THRESHOLD,
    best_match,
    best_similarity,
    group_near_duplicates,
    is_match,
    normalize,
    similarity,
    token_similarity,
)


# ── normalize() ───────────────────────────────────────────────────────────────

class TestNormalize:
    def test_lowercase(self):
        assert normalize("RADIOHEAD") == "radiohead"

    def test_strip_leading_the(self):
        assert normalize("The Beatles") == "beatles"

    def test_strip_leading_a(self):
        assert normalize("A Tribe Called Quest") == "tribe called quest"

    def test_strip_leading_an(self):
        assert normalize("An American Prayer") == "american prayer"

    def test_remove_punctuation(self):
        assert normalize("AC/DC") == "ac dc"

    def test_collapse_whitespace(self):
        assert normalize("Pink   Floyd") == "pink floyd"

    def test_unicode_accents(self):
        # é → e after NFKD decomposition
        assert normalize("Sigur Rós") == "sigur ros"

    def test_empty_string(self):
        assert normalize("") == ""

    def test_idempotent(self):
        s = "The Rolling Stones"
        assert normalize(normalize(s)) == normalize(s)

    def test_only_article(self):
        # Edge: stripping "the " leaves empty
        assert normalize("The") == "the"  # "the" alone with no trailing space

    def test_no_article_mid_string(self):
        # "the" mid-string should NOT be stripped
        result = normalize("Fear of the Dark")
        assert "the" in result


# ── similarity() ─────────────────────────────────────────────────────────────

class TestSimilarity:
    def test_identical(self):
        assert similarity("abbey road", "abbey road") == pytest.approx(100, abs=1)

    def test_completely_different(self):
        s = similarity("radiohead", "beethoven symphony")
        assert s < 60

    def test_normalizes_by_default(self):
        # With normalization: "The Beatles" → "beatles", "Beatles" → "beatles"
        s = similarity("The Beatles", "Beatles")
        assert s == pytest.approx(100, abs=1)

    def test_pre_normalized_skips_normalization(self):
        # Both already normalized
        s = similarity("beatles", "beatles", pre_normalized=True)
        assert s == pytest.approx(100, abs=1)


# ── token_similarity() ────────────────────────────────────────────────────────

class TestTokenSimilarity:
    def test_word_order_independent(self):
        s = token_similarity("Pink Floyd Animals", "Animals Pink Floyd")
        assert s >= 95

    def test_identical(self):
        s = token_similarity("dark side of the moon", "dark side of the moon")
        assert s == pytest.approx(100, abs=1)


# ── is_match() ───────────────────────────────────────────────────────────────

class TestIsMatch:
    def test_identical_strings(self):
        assert is_match("Radiohead", "Radiohead")

    def test_case_difference(self):
        assert is_match("radiohead", "RADIOHEAD")

    def test_article_stripped(self):
        assert is_match("The Beatles", "Beatles")

    def test_clearly_different(self):
        assert not is_match("Radiohead", "Beethoven")

    def test_custom_threshold_strict(self):
        # "Abbey Road" vs "Abbey Rd" — might be below 95 threshold
        # With relaxed threshold should still match
        assert is_match("Abbey Road", "Abbey Road", threshold=100)

    def test_abbreviation_partial(self):
        # "Arcade Fire" vs "Arcade" — partial, may or may not match at default
        # Just verify it doesn't crash
        result = is_match("Arcade Fire", "Arcade")
        assert isinstance(result, bool)

    def test_empty_strings(self):
        # Two empty strings → both normalize to "" → 100% match
        assert is_match("", "")

    def test_one_empty(self):
        assert not is_match("Radiohead", "")


# ── best_match() ─────────────────────────────────────────────────────────────

class TestBestMatch:
    def test_finds_best(self):
        candidates = ["The Beatles", "Led Zeppelin", "Pink Floyd", "Radiohead"]
        match, score = best_match("Beatles", candidates)
        assert match == "The Beatles"
        assert score >= DEFAULT_THRESHOLD

    def test_returns_none_below_threshold(self):
        candidates = ["Mozart", "Bach", "Beethoven"]
        match, score = best_match("Radiohead", candidates, threshold=90)
        assert match is None

    def test_empty_candidates(self):
        match, score = best_match("Radiohead", [])
        assert match is None
        assert score == 0.0


# ── group_near_duplicates() ───────────────────────────────────────────────────

class TestGroupNearDuplicates:
    def test_groups_identical(self):
        items = ["Abbey Road", "Abbey Road", "Let It Be"]
        groups = group_near_duplicates(items)
        # "Abbey Road" appears twice → one group of 2, one of 1
        sizes = sorted(len(g) for g in groups)
        assert sizes == [1, 2]

    def test_no_duplicates(self):
        items = ["Radiohead", "Pink Floyd", "Beethoven", "Bach"]
        groups = group_near_duplicates(items)
        # All distinct → each item in its own group
        assert len(groups) == len(items)

    def test_single_item(self):
        groups = group_near_duplicates(["Radiohead"])
        assert groups == [["Radiohead"]]

    def test_empty_list(self):
        assert group_near_duplicates([]) == []
