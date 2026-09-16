"""One ruling on album capitalisation, so the library stops filing an album twice.

Grey, 2026-09-16: "can we set a standard on At vs at, i like at."

28 case-duplicate album folders per tier -- "Back in Black" beside "Back In
Black" -- which on a folder-browsed library is two albums and in the car is
two entries. Merging needs an answer to "which spelling is right", and the
answer has to live in one place.
"""

from __future__ import annotations

import pytest

from musaeus.title_case import MINOR_WORDS, album_title_case


class TestGreysRule:
    @pytest.mark.parametrize(
        "given,wanted",
        [
            ("Back In Black", "Back in Black"),
            ("Highway To Hell", "Highway to Hell"),
            ("Eat To the Beat", "Eat to the Beat"),
            ("Toys In the Attic", "Toys in the Attic"),
            ("Eye In The Sky", "Eye in the Sky"),
            ("Scenes From The Southside", "Scenes from the Southside"),
        ],
    )
    def test_a_minor_word_inside_the_title_is_lowercased(self, given, wanted):
        assert album_title_case(given) == wanted

    def test_an_already_correct_title_is_unchanged(self):
        assert album_title_case("Back in Black") == "Back in Black"

    def test_it_is_idempotent(self):
        once = album_title_case("Back In Black")
        assert album_title_case(once) == once

    def test_an_all_lowercase_title_is_fixed(self):
        assert album_title_case("magical mystery tour") == "Magical Mystery Tour"


class TestTheEndsAlwaysKeepTheirCapital:
    def test_a_minor_word_first_is_capitalised(self):
        assert album_title_case("the wall") == "The Wall"

    def test_a_minor_word_last_is_capitalised(self):
        """"The Best Of" must not end on a lowercase "of"."""
        assert album_title_case("The Best Of") == "The Best Of"

    def test_a_minor_word_after_a_dash_starts_a_new_phrase(self):
        assert album_title_case(
            "Live 1964 - Concert At Philharmonic Hall"
        ) == "Live 1964 - Concert at Philharmonic Hall"


class TestWhatItMustNotTouch:
    """Blind .title() is what produced half the mess this fixes."""

    @pytest.mark.parametrize(
        "name",
        ["Born in the U.S.A", "R.E.M. Live", "ABBA Gold", "CCR Chronicle", "MTV Unplugged"],
    )
    def test_an_acronym_survives(self, name):
        assert album_title_case(name) == name

    @pytest.mark.parametrize(
        "name", ["McCartney", "DeBarge Anthology", "MacArthur Park", "O'Brien Live"]
    )
    def test_an_interior_capital_survives(self, name):
        """Mccartney is wrong and looks careless."""
        assert album_title_case(name) == name

    @pytest.mark.parametrize(
        "name", ["Blink-182 Greatest", "The Bootleg Series, Vol. 6", "1984", "Sgt. Pepper"]
    )
    def test_anything_with_a_digit_is_left_alone(self, name):
        assert album_title_case(name) == name

    def test_so_keeps_its_capital(self):
        """"so" is a conjunction in "and so to bed" and an adverb in "She's So
        Unusual", and nothing here can tell them apart. It is deliberately not
        a minor word, which is right for the album the library holds."""
        assert "so" not in MINOR_WORDS
        assert album_title_case("She's so Unusual") == "She's So Unusual"


class TestItDoesNotMangleSpacing:
    def test_original_spacing_is_reproduced(self):
        assert album_title_case("A  B") == "A  B"

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_empty_input_is_returned_as_given(self, value):
        assert album_title_case(value) == value

    def test_punctuation_only_is_untouched(self):
        assert album_title_case("!!!") == "!!!"

    def test_a_bracketed_word_is_still_cased(self):
        assert album_title_case("greatest hits (live)") == "Greatest Hits (Live)"
