"""The two forms of an artist name.

MUSAEUS stored "Stooges, The" in the `artist` tag; MusicBrainz only knows
"The Stooges". Measured 2026-08-29: 376 of 839 cached misses were in
article-suffix form and 0 of 2,158 hits were. The sort form belongs in
`soar`, which is what that atom is for.

This rule has regressed three times -- the "(the)" parenthetical, the
"Beatles, The (the)" double spelling, and De La Soul split into
"La Soul, De" -- so the round-trip and the protected names are pinned here.
"""

from __future__ import annotations

import pytest

from musaeus.artist_form import (
    has_article,
    natural_form,
    sort_form,
    tag_values,
)

ARTICLE_NAMES = [
    ("Stooges, The", "The Stooges", "Stooges, The"),
    ("The Stooges", "The Stooges", "Stooges, The"),
    ("Beatles, The", "The Beatles", "Beatles, The"),
    ("Cranberries, The", "The Cranberries", "Cranberries, The"),
    ("A Tribe Called Quest", "A Tribe Called Quest", "Tribe Called Quest, A"),
]

# Stylized names whose leading word only looks like an article.
PROTECTED = ["De La Soul", "Los Lobos", "La Roux", "Los Lonely Boys"]

NO_ARTICLE = ["Refused", "Dusty Springfield", "AC/DC", "TLC"]


@pytest.mark.parametrize("stored,natural,sort", ARTICLE_NAMES)
def test_both_directions(stored, natural, sort):
    assert natural_form(stored) == natural
    assert sort_form(stored) == sort


@pytest.mark.parametrize("stored,natural,sort", ARTICLE_NAMES)
def test_each_form_is_idempotent(stored, natural, sort):
    """Applying a transform to its own output must change nothing."""
    assert natural_form(natural) == natural
    assert sort_form(sort) == sort


@pytest.mark.parametrize("stored,natural,sort", ARTICLE_NAMES)
def test_the_round_trip_closes(stored, natural, sort):
    """natural -> sort -> natural, and back, without drift."""
    assert natural_form(sort_form(natural)) == natural
    assert sort_form(natural_form(sort)) == sort


def test_the_parenthetical_form_is_converted_not_accepted():
    """"(the)" is a real but WRONG form; it regressed three times."""
    assert natural_form("Archies (the)") == "The Archies"
    assert sort_form("Archies (the)") == "Archies, The"


def test_the_double_spelling_does_not_produce_two_articles():
    """"Beatles, The (the)" must not become "The Beatles, The"."""
    assert natural_form("Beatles, The (the)") == "The Beatles"
    assert sort_form("Beatles, The (the)") == "Beatles, The"


@pytest.mark.parametrize("name", PROTECTED)
def test_a_stylized_name_survives_both_directions(name):
    """"De La Soul" -> "La Soul, De" was live corruption, 2026-08-16."""
    assert natural_form(name) == name
    assert sort_form(name) == name
    assert not has_article(name)


@pytest.mark.parametrize("name", NO_ARTICLE)
def test_a_name_with_no_article_is_unchanged(name):
    assert natural_form(name) == name
    assert sort_form(name) == name
    assert not has_article(name)


@pytest.mark.parametrize("stored,natural,sort", ARTICLE_NAMES)
def test_has_article_detects_the_real_ones(stored, natural, sort):
    assert has_article(stored)


def test_empty_input_is_handled_everywhere():
    assert natural_form("") == ""
    assert sort_form("") == ""
    assert not has_article("")
    assert tag_values("") == {}
    assert tag_values(None) == {}


def test_whitespace_is_stripped():
    assert natural_form("  Stooges, The  ") == "The Stooges"
    assert sort_form("  The Stooges  ") == "Stooges, The"


# ── the field split this exists to serve ──────────────────────────────────────


def test_tag_values_puts_each_form_in_its_own_field():
    assert tag_values("Stooges, The") == {
        "artist": "The Stooges",
        "sort_artist": "Stooges, The",
    }


def test_tag_values_accepts_either_input_form():
    """A library mid-migration holds both; the answer must not depend on it."""
    assert tag_values("Stooges, The") == tag_values("The Stooges")


def test_tag_values_for_a_plain_name_are_identical():
    """The caller can then skip writing a redundant sort tag."""
    v = tag_values("Refused")
    assert v["artist"] == v["sort_artist"] == "Refused"
