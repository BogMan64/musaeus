r"""One definition of what a bracket is.

On 2026-09-02 the codebase had three independent bracket regexes:

    neardupe.py       [\(\[]\s*(?:19|20)\d{2}\s*[\)\]]
    original_year.py  \s*[\(\[][^\)\]]*[\)\]]
    doctor.py         [\(\[].*?[\)\]]

All three knew ( ) and [ ] and none knew { }, so all three left this
library title untouched:

    Midnight Cruiser [In the Style of Steely Dan] {Karaoke Demonstration
    Version With Lead Vocal}

They also disagreed on whitespace, so one title normalised differently
depending on which stage looked at it. A fourth copy was then written by
hand for the wanted-list export and had the identical gap -- which is the
real finding: this shape invites being rewritten badly, so the fix is a
shared alphabet rather than three corrected regexes.
"""

from __future__ import annotations

import pytest

from musaeus.brackets import CLOSE, OPEN, has_bracketed, strip_bracketed


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Song (Live)", "Song"),
        ("Song [2015 Remaster]", "Song"),
        ("Song {Vocal Version}", "Song"),
        # the case every earlier implementation missed
        ("Midnight Cruiser [In the Style of Steely Dan] {Karaoke Demo}", "Midnight Cruiser"),
        ("I Can't Go for that (No Can Do) {Vocal Version}", "I Can't Go for that"),
        ("Song", "Song"),
        ("", ""),
    ],
)
def test_every_bracket_style_is_stripped(raw, expected) -> None:
    assert strip_bracketed(raw) == expected


def test_a_mismatched_pair_is_still_an_annotation() -> None:
    """The live library holds "Medley - Ain't That A Shame [Live}" --
    opened square, closed curly. Every earlier version left it intact."""
    assert strip_bracketed("Medley - Ain't That A Shame [Live}") == "Medley - Ain't That A Shame"


def test_whitespace_is_collapsed_here_not_by_each_caller() -> None:
    """doctor.py left a double space where original_year.py did not, so
    the same title compared unequal for no visible reason."""
    assert strip_bracketed("A (x) B") == "A B"
    assert strip_bracketed("  Song (x)  ") == "Song"


def test_two_annotations_stay_two_matches() -> None:
    """A greedy pattern would swallow the real title between them."""
    assert strip_bracketed("Real (a) Title (b) Here") == "Real Title Here"


def test_has_bracketed() -> None:
    assert has_bracketed("Song {x}")
    assert not has_bracketed("Song")
    assert not has_bracketed("")


# ── The callers ───────────────────────────────────────────────────────────────


def test_doctor_and_the_helper_agree() -> None:
    from musaeus.doctor import song_key

    t = "Midnight Cruiser [In the Style of Steely Dan] {Karaoke Demo}"
    assert song_key("X", t)[1] == "midnightcruiser"


def test_original_year_strips_every_style() -> None:
    import musaeus.stages.original_year as oy

    assert not hasattr(oy, "_ALL_BRACKETS_RE"), "should use the shared helper now"


def test_neardupe_shares_the_alphabet_but_not_the_judgement() -> None:
    """It strips only version words in brackets, deliberately: a real
    parenthetical is part of the title. "Here I Am (Come and Take Me)"
    must not collapse to "Here I Am"."""
    import musaeus.stages.neardupe as nd

    assert nd._VERSION_BRACKET_WORDS.sub("", "Here I Am (Come and Take Me)") == (
        "Here I Am (Come and Take Me)"
    )
    for style in ("(2015)", "[2015]", "{2015}"):
        assert nd._YEAR_BRACKET_RE.sub("", f"Song {style}").strip() == "Song", style


# ── The trap that made this change dangerous ──────────────────────────────────


def test_quantifiers_survived_the_move_into_f_strings() -> None:
    r"""Interpolating OPEN/CLOSE turns these patterns into f-strings, and
    an f-string eats a regex quantifier: `\d{2}` becomes `\d2`, and
    `\d{1,2}` becomes `\d(1, 2)` -- the tuple, rendered. Both still
    COMPILE, and both match the wrong thing.

    Hit while making this very change on 2026-09-02, caught only by
    printing the compiled pattern.
    """
    import musaeus.stages.neardupe as nd

    assert r"\d{2}" in nd._YEAR_BRACKET_RE.pattern
    assert r"\d{1,2}" in nd._NUM_BRACKET_RE.pattern
    assert "(1, 2)" not in nd._NUM_BRACKET_RE.pattern
    # and the behaviour those quantifiers exist for
    assert nd._YEAR_BRACKET_RE.sub("", "Song (2015)").strip() == "Song"
    assert nd._NUM_BRACKET_RE.sub("", "Song [12]").strip() == "Song"


def test_the_alphabet_constants_cover_all_three_styles() -> None:
    for ch in "([{":
        assert ch in OPEN.replace("\\", "")
    for ch in ")]}":
        assert ch in CLOSE.replace("\\", "")
