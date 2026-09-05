"""Title-casing must not overwrite deliberate spelling.

2026-09-05. Consolidating artist variants pushed the winning name through
_smart_title(), whose word loop did `word[:1].upper() + word[1:].lower()`.
That flattened every interior capital: SwitchOTR became Switchotr in a live
run, and measured against the real library the same loop would have rewritten
202 artists across 477 tracks -- "Loreena Mckennitt" (33 tracks), "Bobby
Mcferrin", "Sarah Mclachlan", "Bachman-turner Overdrive", "Kool & the Gang".

This only bites when a variant group forms with no explicit canon entry to
win instead, which is why it went unnoticed: the canon protected the artists
anyone had already thought about.
"""

from __future__ import annotations

import pytest

from musaeus.stages.artist_consolidate import _smart_title


class TestInteriorCapitalsSurvive:
    """An interior capital is spelling, not sloppiness."""

    @pytest.mark.parametrize("name", [
        "Loreena McKennitt", "Bobby McFerrin", "Sarah McLachlan", "Don McLean",
        "Michael McDonald", "Ashley MacIsaac", "Jackie DeShannon", "DeBarge",
        "SwitchOTR", "R.E.M.", "T-Bone Walker", "LCD Soundsystem", "iZombie",
    ])
    def test_a_name_with_an_interior_capital_is_returned_unchanged(self, name):
        assert _smart_title(name) == name


class TestHyphenatedPartsAreCapitalised:
    @pytest.mark.parametrize("raw,want", [
        ("bachman-turner overdrive", "Bachman-Turner Overdrive"),
        ("buffy sainte-marie", "Buffy Sainte-Marie"),
        ("salt-n-pepa", "Salt-N-Pepa"),
        ("vanessa-mae", "Vanessa-Mae"),
    ])
    def test_each_part_of_a_hyphenated_word_is_capitalised(self, raw, want):
        assert _smart_title(raw) == want


class TestConnectorWordsOnlyLowercaseMidName:
    """"The" after "&" or a comma starts a new band name, so it keeps its
    capital. "Kool & the Gang" was the live symptom."""

    @pytest.mark.parametrize("raw,want", [
        ("kool & the gang", "Kool & The Gang"),
        ("sly & the family stone", "Sly & The Family Stone"),
        ("hootie & the blowfish", "Hootie & The Blowfish"),
        ("echo & the bunnymen", "Echo & The Bunnymen"),
        ("gerry & the pacemakers", "Gerry & The Pacemakers"),
    ])
    def test_the_keeps_its_capital_after_an_ampersand(self, raw, want):
        assert _smart_title(raw) == want

    def test_a_connector_still_lowercases_mid_name(self):
        """The rule that already worked must keep working."""
        assert _smart_title("of monsters and men") == "Of Monsters and Men"
        assert _smart_title("the lord of the rings") == "The Lord of the Rings"

    def test_a_leading_connector_is_not_lowercased(self):
        assert _smart_title("the KLF").startswith("The")


class TestPreviouslyWorkingBehaviourIsUnchanged:
    """Regression net for the cases the old loop got right."""

    @pytest.mark.parametrize("raw,want", [
        ("ABBA", "ABBA"),
        ("ZZ Top", "ZZ Top"),
        ("earth, wind & fire", "Earth, Wind & Fire"),
        # a trailing ", The" is the article suffix and stays capitalised --
        # the live bug that put a lowercase "the" on Bob Seger System
        ("bob seger system, The", "Bob Seger System, The"),
        ("chieftains and belfast harp orchestra, The",
         "Chieftains and Belfast Harp Orchestra, The"),
    ])
    def test_unchanged(self, raw, want):
        assert _smart_title(raw) == want
