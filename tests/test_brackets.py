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


def test_song_key_reads_ampersand_and_the_word_and_as_one():
    # Doctor called "Jack And Diane" a sole copy while "Jack & Diane" sat in
    # the library (2026-10-18).
    from musaeus.doctor import song_key

    assert song_key("John Mellencamp", "Jack And Diane") == song_key(
        "John Mellencamp", "Jack & Diane"
    )
    assert song_key("Simon & Garfunkel", "X") == song_key("Simon and Garfunkel", "X")


def test_song_key_of_a_bare_ampersand_is_not_the_song_and():
    # Codex review of #209: a quarantined "&" looked held by a catalogued "And".
    from musaeus.doctor import song_key

    assert song_key("Wings", "&") != song_key("Wings", "And")


def test_the_word_and_is_never_dropped_from_a_song_key():
    # Codex review of #209: dropping the WORD "and" made "And I Love Her" look
    # held by "I Love Her", so a quarantined sole copy passed as a duplicate.
    from musaeus.doctor import song_key

    assert song_key("X", "And I Love Her") != song_key("X", "I Love Her")


def test_a_tag_that_lost_its_ampersand_is_still_held():
    # "Hall Oates" matched "Hall & Oates" while "&" was dropped; it still does.
    from musaeus.doctor import song_keys

    assert set(song_keys("Hall & Oates", "Maneater")) & set(song_keys("Hall Oates", "Maneater"))
    assert not set(song_keys("X", "And I Love Her")) & set(song_keys("X", "I Love Her"))


def test_artist_and_title_spellings_are_chosen_independently():
    # Codex review of #209: "Hall & Oates - Rock & Roll" against "Hall and
    # Oates - Rock Roll" shared no key.
    from musaeus.doctor import song_keys

    assert set(song_keys("Hall & Oates", "Rock & Roll")) & set(
        song_keys("Hall and Oates", "Rock Roll")
    )


def test_a_version_ending_is_set_aside_and_a_part_is_kept():
    # Codex review of #216, round two: "Song - Part 1 Live" is the doctor's
    # "Song - Part 1"; the part stays, the version goes.
    from musaeus.brackets import base_title
    from musaeus.doctor import song_key

    assert base_title("Song - Part 1 Live") == "Song - Part 1"
    assert base_title("Layla - Acoustic Live") == "Layla"
    assert song_key("A", "Song - Part 1 Live") == song_key("A", "Song - Part 1")
    assert song_key("A", "Song - Part 1") != song_key("A", "Song - Part 2")


def test_a_part_in_brackets_is_kept_in_the_song_key():
    # Review of #216: brackets went with the part inside them, so a missing
    # "(Part 2)" looked held by "(Part 1)".
    from musaeus.doctor import song_key

    assert song_key("A", "Song (Part 1)") != song_key("A", "Song (Part 2)")
    assert song_key("A", "Song (Part 1)") == song_key("A", "Song - Part 1")
    assert song_key("A", "Song (Pt. 1) (Live)") == song_key("A", "Song (Part 1)")


@pytest.mark.parametrize(
    "bracketed, dashed",
    [("Song (Pt. 1)", "Song - Pt. 1"), ("Song (Part II)", "Song - Part II"),
     ("Song (Part One)", "Song - Part One Live"),
     # Codex review of #217, round 2: the part keeps its place in the title.
     ("Song (Part 1) and Part 2", "Song Part 1 and Part 2")],
)  # fmt: skip
def test_a_part_is_one_song_key_in_any_spelling(bracketed, dashed):
    # Codex review of #217: the part from brackets was spelled "Part 1", the
    # one after a dash was left as written, so one recording had two keys.
    from musaeus.doctor import song_key

    assert song_key("A", bracketed) == song_key("A", dashed)


def test_a_part_after_a_version_ending_is_kept():
    # Codex review of #217, round 3: the version ending took the part after it.
    from musaeus.doctor import song_key

    assert song_key("A", "Song - Live (Part 1)") != song_key("A", "Song - Live (Part 2)")
    assert song_key("A", "Song - Live (Part 1)") == song_key("A", "Song - Part 1")
    assert song_key("A", "Song - Part 1 2011 Remaster") == song_key("A", "Song - Part 1")
