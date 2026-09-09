"""A protected artist is a NAME, not a fragment that appears in one.

PROTECTED_ARTISTS was checked with `protected in artist_lower`, a
substring test, which inverted the list's purpose in both directions.

It protected junk. "neil young" is on the list so that Neil Young's own
tracks on tribute compilations survive -- and it was therefore also
shielding "The Neil Young Tribute Band", four tracks of which sat in the
library on 2026-09-01 passing as a real artist. "sleep" (the doom metal
band) was shielding "Deep Sleep Music Collective".

And it made junk patterns unreachable: \\bsleep\\b and \\bhealing\\b could
never fire, because any artist they matched contained a protected word by
definition.

The fix is to match the whole name. What it must NOT do is start catching
real records that merely contain a trigger word, so those are pinned here
too -- Asleep at the Wheel, Sleeping at Last, the Beatles' "I'm Only
Sleeping". They are safe because the junk patterns are word-bounded, not
because anything protects them, and that is worth a test.
"""

from __future__ import annotations

import pytest

from musaeus.stages.tribute_quarantine import PROTECTED_ARTISTS, is_junk

# ── The list protects the artist it names ─────────────────────────────────────


@pytest.mark.parametrize(
    "artist,title,album",
    [
        ("Neil Young", "Old Man", "A Tribute to Neil Young"),
        ("Louis Armstrong", "Honeysuckle Rose", "Satch Plays Fats - A Tribute"),
        ("Garth Brooks", "The Dance", "Garth Brooks Hits - A Tribute Album"),
        ("Sleep", "Dopesmoker", "Dopesmoker"),
        ("Jan & Dean", "Ride the Wild Surf (instrumental version)", ""),
    ],
)
def test_a_named_artist_is_protected(artist, title, album) -> None:
    junk, why = is_junk(artist, title, album)
    assert not junk, f"{artist} was quarantined as {why}"


def test_article_forms_are_the_same_artist() -> None:
    """The library stores "Pretenders, The"; MusicBrainz says "The
    Pretenders". A protected entry written either way must match both."""
    for name in ("Monks", "The Monks", "Monks, The"):
        assert not is_junk(name, "Complication", "A Tribute To the Monks")[0], name


# ── ...and not everyone who contains its name ─────────────────────────────────


def test_a_tribute_band_is_not_protected_by_the_artist_it_imitates() -> None:
    """The exact inversion: the entry that keeps Neil Young's own records
    was keeping a band that exists to imitate him."""
    junk, why = is_junk("Neil Young Tribute Band, The", "Old Man", "")
    assert junk and "tribute" in why


def test_a_protected_word_inside_a_junk_name_does_not_shield_it() -> None:
    junk, _ = is_junk("Deep Sleep Music Collective", "Meditation Music With Water", "")
    assert junk


@pytest.mark.parametrize(
    "pattern_word,junk_artist",
    [("sleep", "Sleep Meditation Music"), ("healing", "Healing Vibrations")],
)
def test_the_shadowed_patterns_can_now_fire(pattern_word, junk_artist) -> None:
    """\\bsleep\\b and \\bhealing\\b were unreachable while any artist they
    matched was protected by substring."""
    assert is_junk(junk_artist, "Some Track", "")[0], pattern_word


# ── Real records with a trigger word in the name ──────────────────────────────


@pytest.mark.parametrize(
    "artist,title",
    [
        ("Asleep At The Wheel", "Hot Rod Lincoln"),
        ("Sleeping at Last", "Saturn"),
        ("ZZ Top", "Sleeping Bag"),
        ("Beatles, The", "I'm Only Sleeping"),
        ("Barenaked Ladies", "Who Needs Sleep"),
        ("Pretenders, The", "I Go to Sleep"),
        ("Tokens, The", "The Lion Sleeps Tonight (Wimoweh)"),
        ("Billie Eilish", "bad guy"),
        ("Biz Markie", "Just a Friend"),
    ],
)
def test_real_music_with_a_trigger_word_survives(artist, title) -> None:
    """These are safe because the junk patterns are word-bounded, NOT
    because anything protects them. If a pattern ever loses its \\b, this
    is what fails."""
    junk, why = is_junk(artist, title, "")
    assert not junk, f"{artist} - {title} would be quarantined as {why}"


def test_the_bare_jan_entry_is_gone() -> None:
    """It protected every artist with those three letters anywhere in the
    name. The canonical form is what the artist canon settles on."""
    assert "jan" not in PROTECTED_ARTISTS
    assert "jan & dean" in PROTECTED_ARTISTS


# ── Both sides of the comparison must be keyed the same way ───────────────────
#
# Found 2026-09-02 by auditing for duplicated text logic. The 2026-09-01
# version article-stripped only the ARTIST and compared it against the raw
# PROTECTED_ARTISTS list. So "Healing, The" reduced to "healing", missed the
# protected entry "the healing", and was quarantined as junk by the
# \bhealing\b pattern -- a real band, moved out of the library, by a guard
# written to protect it.
#
# The article rule also already existed in artist_form.py, whose own
# docstring says "the honest test is that the transforms disagree, not a
# regex of our own". A fresh regex was written anyway.


@pytest.mark.parametrize(
    "stored,leading",
    [("Healing, The", "The Healing"), ("Monks, The", "The Monks"), ("Sleep", "Sleep")],
)
def test_both_article_forms_reach_the_protected_entry(stored, leading) -> None:
    """The library stores the comma form; MusicBrainz returns the leading
    form. A protected entry written either way has to match both."""
    assert not is_junk(stored, "Some Song", "A Tribute Album")[0], stored
    assert not is_junk(leading, "Some Song", "A Tribute Album")[0], leading


def test_the_protected_list_is_keyed_by_the_same_transform() -> None:
    """Deriving the set through comparison_key is what makes it impossible
    to strip one side and not the other."""
    from musaeus.artist_form import comparison_key
    from musaeus.stages.tribute_quarantine import _PROTECTED_KEYS, PROTECTED_ARTISTS

    assert frozenset(comparison_key(p) for p in PROTECTED_ARTISTS) == _PROTECTED_KEYS
    assert comparison_key("Healing, The") in _PROTECTED_KEYS


def test_a_comma_that_is_not_an_article_is_not_truncated() -> None:
    """has_article() guards the split, so "Peter, Paul and Mary" does not
    become "Peter" -- which would key it to a different act entirely."""
    from musaeus.artist_form import comparison_key

    assert comparison_key("Peter, Paul and Mary") == "peter, paul and mary"


def test_the_hand_rolled_article_regex_is_gone() -> None:
    import musaeus.stages.tribute_quarantine as tq

    assert not hasattr(tq, "_strip_article"), "article logic belongs in artist_form"
