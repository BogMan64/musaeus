"""
Tests for SpellCheckStage.

Grey has dyslexia and asked for a spelling pass. A dictionary is the
wrong tool -- artist names are proper nouns and routinely odd ("a-ha",
"Ne-Yo", "NSYNC") -- so this matches names against other names in the
corpus instead. The question is never "is this a word" but "is there a
near-identical name that IS real".

It is report-only, and its own first live run justified that: it flagged
"Sonny Boy Williamson" against "Sonny Boy Williamson II" and "Hank
Williams" against "Hank Williams Jr" at scores of 92-95. Both pairs are
different people. Auto-renaming on a similarity score would have merged
them.
"""

from __future__ import annotations

from musaeus.stages.spellcheck import _norm, find_suspects


def test_catches_a_plain_misspelling():
    """The case Grey asked for: Deperados is wrong only because Desperados exists."""
    out = find_suspects({"Desperados": 12, "Deperados": 1})
    assert len(out) == 1
    suspect, s_files, correct, c_files, _ = out[0]
    assert suspect == "Deperados"
    assert correct == "Desperados"


def test_the_rarer_spelling_is_the_suspect():
    """A typo appears once; the real name carries the catalogue."""
    out = find_suspects({"Ricky Nelson": 30, "Rick Nelson": 1})
    assert out[0][0] == "Rick Nelson"


def test_equal_weight_is_not_reported():
    """With no evidence either way there is nothing to suggest."""
    assert find_suspects({"Tornados, The": 3, "Tornadoes, The": 3}) == []


def test_identical_after_normalisation_is_not_a_typo():
    """Same name, different punctuation -- nothing to suggest."""
    assert _norm("Bill Haley & His Comets") == _norm("Bill Haley and His Comets")
    assert find_suspects({"Bill Haley & His Comets": 5, "Bill Haley and His Comets": 1}) == []


def test_stylised_spelling_is_flagged_as_a_suspect():
    """Payola$ vs Payolas ARE reported, and should be.

    The "$" is stripped rather than read as an "s", so the two do not
    normalise equal -- and that is the right outcome: they are the same
    band under a stylised name, which is exactly the merge worth
    offering. Written down because the opposite looks plausible and I
    asserted it the wrong way round first.
    """
    out = find_suspects({"Payola$, The": 3, "Payolas, The": 9})
    assert len(out) == 1
    assert out[0][0] == "Payola$, The"
    assert out[0][2] == "Payolas, The"


def test_ampersand_and_and_are_the_same_word():
    assert _norm("Bill Haley & His Comets") == _norm("Bill Haley and His Comets")


def test_known_distinct_artists_are_never_suggested():
    """Different people who happen to score high.

    Both pairs came out of this stage's own first live run at 92-95.
    """
    assert find_suspects({"Hank Williams": 40, "Hank Williams Jr": 2}) == []
    assert find_suspects({"Sonny Boy Williamson": 1, "Sonny Boy Williamson II": 40}) == []
    assert find_suspects({"Paul Young": 10, "John Paul Young": 1}) == []


def test_genuinely_different_names_score_below_threshold():
    assert find_suspects({"Queen": 75, "Santana": 63}) == []
    assert find_suspects({"The Beatles": 50, "The Monkees": 5}) == []


def test_deliberately_odd_names_are_left_alone():
    """A dictionary would flag every one of these; this must not."""
    odd = {"a-ha": 7, "Ne-Yo": 3, "NSYNC": 3, "Static-X": 1, "Tone-Loc": 4}
    assert find_suspects(odd) == []


def test_empty_and_single_artist_libraries_are_safe():
    assert find_suspects({}) == []
    assert find_suspects({"Solo": 1}) == []
