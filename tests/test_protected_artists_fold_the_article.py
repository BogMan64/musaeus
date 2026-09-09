"""Artist protection must survive the article convention.

M-04 in the Repair Register, 2026-09-08. Confirmed by execution, not by
reading.

`is_protected()` lowercased and stripped, and nothing else. The canon holds
`"andrews sisters (the)"`, so:

    is_protected("Andrews Sisters (the)")  -> True
    is_protected("Andrews Sisters, The")   -> False
    is_protected("The Andrews Sisters")    -> False
    is_protected("Andrews Sisters")        -> False

Only the True one is a spelling this library never produces. And it is worse
than a miss: `normalize.py`'s article repair rewrites names *into* the
suffix form, so the pipeline actively converted the one working spelling
into a non-working one before anything asked whether it was protected.

Measured on the live library the day it was fixed: of the thirteen canon
entries, exactly one was dormant — and the library stores that artist as
**"Andrews Sisters, The", 4 rows**, which is precisely the spelling that
returned False. The other twelve matched their stored spelling already, so
the blast radius was one artist. The *failure mode* is the systemic part: a
dormant rule looks exactly like an absent one, so nothing reported it.

`GenreLaw._key()` had already learned this. Its docstring records 246 genre
rules dormant for the same reason. The fix is to use that key rather than to
restate the folding here — two modules folding differently is the incident
itself, not a step towards fixing it.
"""

from __future__ import annotations

import sqlite3

import pytest

from musaeus.canon.genre_law import GenreLaw
from musaeus.canon.protected_artists import (
    PROTECTED_ARTIST_NAMES,
    is_protected,
)


@pytest.mark.parametrize("spelling", [
    "Andrews Sisters (the)",
    "Andrews Sisters, The",
    "The Andrews Sisters",
    "Andrews Sisters",
    "  andrews   sisters ,  the  ",
])
def test_every_article_spelling_is_protected(spelling: str) -> None:
    assert is_protected(spelling) is True, spelling


def test_the_spelling_the_library_actually_stores_is_protected() -> None:
    """The one that mattered. normalize.py produces this form."""
    assert is_protected("Andrews Sisters, The") is True


def test_a_name_not_in_the_canon_is_still_unprotected() -> None:
    """The fold must not turn into 'protect everything'."""
    for name in ["Nobody At All", "The Beatles", "Beatles, The", "", None]:
        assert is_protected(name) is False, name


def test_ampersands_are_not_folded_into_and() -> None:
    """"Of Monsters and Men" spells its own name with "and".

    The canon comments say a blanket ampersand rule would be wrong. Folding
    & into and here would merge that entry with a hypothetical "&" spelling
    and recreate the collision those comments warn about -- and it would
    quietly protect a name nobody listed.
    """
    assert GenreLaw._key("Of Monsters & Men") != GenreLaw._key("Of Monsters and Men")
    assert is_protected("Of Monsters and Men") is True
    assert is_protected("Of Monsters & Men") is False


def test_protection_agrees_with_the_key_it_borrows() -> None:
    """Pins the coupling, so the two cannot drift apart again.

    If someone gives protected_artists its own folding rule, or changes
    GenreLaw._key without looking here, this fails -- which is the whole
    point. Restating the rule in two modules is the defect, not the fix.
    """
    for name in PROTECTED_ARTIST_NAMES:
        for variant in (name, name.upper(), f"  {name}  "):
            assert is_protected(variant) is True, variant
        assert GenreLaw._key(name) in {GenreLaw._key(n) for n in PROTECTED_ARTIST_NAMES}


def test_no_canon_entry_is_dormant() -> None:
    """A stored spelling must be reachable through its own front door.

    Before the fix, "andrews sisters (the)" was the only entry whose stored
    form differed from its folded key -- and that difference was the bug.
    Folding makes any such entry work, so this asserts the property that
    matters rather than the spelling: every entry protects itself.
    """
    dormant = [n for n in PROTECTED_ARTIST_NAMES if not is_protected(n)]
    assert dormant == [], dormant


@pytest.mark.skipif(
    not __import__("pathlib").Path(
        "/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/musaeus.db").exists(),
    reason="live vault not present",
)
def test_the_live_library_spellings_are_all_protected() -> None:
    """The measurement that gave this finding its scope.

    Skipped when the vault is absent, so the suite stays portable; run
    against the real library it is the check that actually matters.
    """
    conn = sqlite3.connect(
        "file:/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/musaeus.db?mode=ro", uri=True)
    keys = {GenreLaw._key(n) for n in PROTECTED_ARTIST_NAMES}
    unprotected = [
        a for (a,) in conn.execute(
            "SELECT DISTINCT artist FROM archive WHERE status='CATALOGUED'")
        if a and GenreLaw._key(a) in keys and not is_protected(a)
    ]
    conn.close()
    assert unprotected == [], unprotected
