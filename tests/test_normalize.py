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

import pytest

from musaeus.stages.normalize import (
    PROTECTED_ARTIST_NAMES,
    _move_article_to_suffix,
    _normalise_artist,
    _smart_title_case,
)


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


# ── Parenthetical "(the)" is a WRONG form, not an already-correct one ────────
#
# Regression guard for a bug confirmed live 2026-08-21: _move_article_to_suffix
# OR'd _ARTICLE_SUFFIX_RE and _ARTICLE_COMMA_RE into one "already has suffix
# format, return as-is" check. That classified the parenthetical "(the)" form
# as already-correct, so Normalize passed it through untouched forever --
# 1,262 archive rows across 203 distinct artists stranded in the wrong form.


class TestParentheticalArticleConverted:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Archies (the)", "Archies, The"),
            ("5th Dimension (the)", "5th Dimension, The"),
            ("Animals (the)", "Animals, The"),
            ("Alan Parsons Project (the)", "Alan Parsons Project, The"),
            ("Beatles (The)", "Beatles, The"),
        ],
    )
    def test_parenthetical_converted_to_comma_suffix(self, raw, expected):
        assert _move_article_to_suffix(raw) == expected

    def test_double_form_does_not_stack_a_second_article(self):
        # The documented pathological input. Must collapse to the canonical
        # single suffix, never "Beatles, The, The" or "The Beatles, The".
        assert _move_article_to_suffix("Beatles, The (the)") == "Beatles, The"

    @pytest.mark.parametrize(
        "name",
        [
            "Archies (the)",
            "Beatles, The (the)",
            "5th Dimension (the)",
            "The Beatles",
            "Beatles, The",
            "De La Soul",
            "Los Lobos",
        ],
    )
    def test_conversion_is_rerun_safe(self, name):
        once = _move_article_to_suffix(name)
        assert _move_article_to_suffix(once) == once

    def test_protected_name_still_wins_over_conversion(self):
        # A protected stylized name must never be touched, even now that the
        # parenthetical branch is active.
        assert _move_article_to_suffix("De La Soul") == "De La Soul"


# ── Roman numerals past X, and their real-word collisions ────────────────────


class TestRomanNumerals:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("PART XIV", "Part XIV"),
            ("CHAPTER XII", "Chapter XII"),
            ("ACT III", "Act III"),
            ("SYMPHONY IX", "Symphony IX"),
            ("LED ZEPPELIN IV", "Led Zeppelin IV"),
            # Chicago really did number albums this far.
            ("CHICAGO XXXVIII", "Chicago XXXVIII"),
        ],
    )
    def test_roman_numerals_preserved_beyond_ten(self, raw, expected):
        assert _smart_title_case(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # MIX parses as a valid Roman numeral (M + IX = 1009) and is all
            # over a real music library -- it must stay a word.
            ("RADIO MIX", "Radio Mix"),
            ("EXTENDED MIX", "Extended Mix"),
            ("DIM THE LIGHTS", "Dim the Lights"),
        ],
    )
    def test_real_words_that_parse_as_numerals_are_not_uppercased(self, raw, expected):
        assert _smart_title_case(raw) == expected

    def test_lowercase_numeral_like_word_is_not_promoted(self):
        # Only an already-all-caps token is eligible; a lowercase "mix" must
        # never be promoted to "MIX" just because it parses as a numeral.
        assert "MIX" not in _smart_title_case("the radio mix")


# ── Dotted abbreviations ─────────────────────────────────────────────────────


class TestDottedAbbreviations:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("THE U.S.A. TOUR", "The U.S.A. Tour"),
            ("R.E.M. GREATEST HITS", "R.E.M. Greatest Hits"),
            ("D.O.A.", "D.O.A."),
        ],
    )
    def test_dotted_abbreviations_stay_uppercase(self, raw, expected):
        assert _smart_title_case(raw) == expected

    def test_single_letter_prefix_is_not_treated_as_abbreviation(self):
        # "Mr." is one letter + a period, not a dotted abbreviation -- it must
        # title-case normally rather than becoming "MR.".
        assert _smart_title_case("MR. BIG STUFF") == "Mr. Big Stuff"


class TestCapitalisationIsNotOverwritten:
    """Found 2026-09-05 by normalize's OWN verify_effect, which reported nine
    stored artist names that "would still change if normalized again" -- a
    check reporting on its own stage's residue, which is what it is for.

    Two distinct faults sat behind those nine.
    """

    @pytest.mark.parametrize("name", [
        "R.E.M", "O.S.T", "M.I.A",           # no trailing period -- 29 live tracks
        "R.E.M.", "U.S.A.",                  # with one -- already worked
    ])
    def test_a_dotted_abbreviation_survives_without_a_trailing_period(self, name):
        """_DOTTED_ABBREV_RE required a trailing period, so "R.E.M." was
        protected while "R.E.M" -- the commoner spelling, and 26 tracks in
        the live library -- came out "R.e.m"."""
        assert _normalise_artist(name) in (None, name)

    @pytest.mark.parametrize("name", [
        "Loreena McKennitt", "Bobby McFerrin", "DeBarge", "T-Bone Walker",
    ])
    def test_a_mixed_case_interior_capital_survives(self, name):
        assert _normalise_artist(name) in (None, name)

    @pytest.mark.parametrize("name", ["ABBA", "ZZ Top", "MF"])
    def test_known_acronyms_survive(self, name):
        """Wholly-uppercase tokens cannot be told from shouted words by any
        rule, so these live on _KEEP_CAPS, decided one at a time."""
        assert _normalise_artist(name) in (None, name)

    @pytest.mark.parametrize("raw,want", [
        ("2 LIVE CREW", "2 Live Crew"),
        ("DAVID ROSE", "David Rose"),
    ])
    def test_shouted_words_are_still_title_cased(self, raw, want):
        """The counterpart the first draft of this fix broke: "short and
        all-caps means acronym" preserved ABBA correctly but also left "2
        LIVE CREW" untouched, because LIVE and CREW are equally short and
        equally uppercase. Title-casing those is the point of the stage."""
        assert _normalise_artist(raw) == want
