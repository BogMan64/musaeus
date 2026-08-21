"""
Tests for NormalizeStage's _move_article_to_suffix().

Regression context (2026-08-16): a live Normalize run against the real
21,899-file library reported "Fixed: 0 artist(s)" despite the data
containing 18 distinct lowercase-leading-article artists ("the
Chieftains", "the Band", "the Bangles", ...). Root cause: the article
check `s.startswith(f"{article} ")` was case-sensitive against a
capitalized-only article list, so every lowercase variant silently
passed through unfixed -- not counted as an error, just never touched.

While verifying the fix, a second, independent, pre-existing bug was
found and confirmed as REAL DATA CORRUPTION from that same live run:
"De La Soul" (a real, correctly-spelled band name whose first word
happens to also be a real leading article in 10+ other languages) was
mis-normalized to "La Soul, De" in the live DB (2 real files affected).
This wasn't caused by the case-sensitivity fix -- the original
case-sensitive code already matched "De " exactly, no casing needed.
Fixed via PROTECTED_ARTIST_NAMES, mirroring artist_consolidate.py's
PROTECTED_FULL_ARTIST_NAMES pattern for the identical problem shape.
"""

from musaeus.stages.normalize import PROTECTED_ARTIST_NAMES, _move_article_to_suffix


class TestMoveArticleToSuffixCaseInsensitive:
    """Confirms the case-sensitivity fix: any casing of a leading
    article must be caught, not just exact "The "/"A "/"An "/etc."""

    def test_lowercase_the(self):
        assert _move_article_to_suffix("the Chieftains") == "Chieftains, The"

    def test_lowercase_the_band(self):
        assert _move_article_to_suffix("the Band") == "Band, The"

    def test_all_caps_the(self):
        # _move_article_to_suffix alone doesn't title-case the rest of
        # the string -- that's _smart_title_case()'s job, called earlier
        # in _normalise_artist(). This function's own contract is just
        # "detect and move the article," case-insensitively.
        assert _move_article_to_suffix("THE BEATLES") == "BEATLES, The"

    def test_mixed_case_a(self):
        assert _move_article_to_suffix("a Tribe Called Quest") == "Tribe Called Quest, A"

    def test_already_capitalized_still_works(self):
        """Fail-first sanity: the original capitalized-only cases must
        keep working, not just the newly-fixed lowercase ones."""
        assert _move_article_to_suffix("The Beatles") == "Beatles, The"
        assert _move_article_to_suffix("An American Band") == "American Band, An"

    def test_no_article_unchanged(self):
        assert _move_article_to_suffix("Refused") == "Refused"

    def test_already_suffix_form_unchanged(self):
        assert _move_article_to_suffix("Beatles, The") == "Beatles, The"


class TestMoveArticleToSuffixProtectedNames:
    """Confirms real stylized band names whose leading word coincides
    with an article are never split -- the De La Soul live-corruption
    bug."""

    def test_de_la_soul_not_split(self):
        assert _move_article_to_suffix("De La Soul") == "De La Soul"

    def test_de_la_soul_case_insensitive(self):
        assert _move_article_to_suffix("de la soul") == "de la soul"

    def test_la_roux_not_split(self):
        assert _move_article_to_suffix("La Roux") == "La Roux"

    def test_los_lobos_not_split(self):
        assert _move_article_to_suffix("Los Lobos") == "Los Lobos"

    def test_los_lonely_boys_not_split(self):
        assert _move_article_to_suffix("Los Lonely Boys") == "Los Lonely Boys"

    def test_die_aerzte_not_split(self):
        assert _move_article_to_suffix("Die Ärzte") == "Die Ärzte"

    def test_protected_set_contents(self):
        # Guards against someone silently trimming this list back down
        # without realizing why each entry is there.
        assert "de la soul" in PROTECTED_ARTIST_NAMES
        assert "los lobos" in PROTECTED_ARTIST_NAMES
