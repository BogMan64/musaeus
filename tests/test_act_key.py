"""One act's key, kept once in artist_form (Grey's code review, 2026-10-11,
finding 12: doctor and AcoustID each carried a private copy)."""

from __future__ import annotations

import pytest

from musaeus import doctor
from musaeus.artist_form import act_key
from musaeus.stages import acousticid


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Beyonce", "Beyoncé"),
        ("The Beatles", "Beatles, The"),
        ("Beatles", "The Beatles"),
        ("Hall & Oates", "Hall and Oates"),
        ("Simon + Garfunkel", "Simon and Garfunkel"),
        ("Run-DMC", "Run DMC"),
    ],
)
def test_one_act_keys_alike(a, b):
    assert act_key(a) == act_key(b)


def test_different_acts_stay_apart():
    assert act_key("Dion") != act_key("Celine Dion")
    assert act_key("Peter, Paul and Mary") != act_key("Peter")


@pytest.mark.parametrize("name", ["The Who", "Beyoncé", "Earth, Wind & Fire", "", "AC/DC"])
def test_both_callers_use_the_shared_key(name):
    assert doctor._spelling_key(name) == act_key(name) == acousticid._artist_key(name)
