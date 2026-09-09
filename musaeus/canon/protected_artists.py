#!/usr/bin/env python3
"""
MUSAEUS — Protected artist names: the one home.

Band names that CONTAIN a comma or an ampersand and must never be split,
folded, or re-cased by any stage that rewrites archive.artist. Splitting
"Earth, Wind & Fire" on its comma yields "Earth"; folding "Sly & The Family
Stone" on its ampersand yields "Sly".

Why this file exists
--------------------
The same list was copied into three modules and then DIVERGED. Measured
2026-09-05:

    curator             12 names
    artist_consolidate   9 names
    organize             6 names          <- names your folders
    union               13 names

organize.py was missing seven -- Big Brother & The Holding Company, Dr.
Hook & The Medicine Show, Adam & The Ants, Barney Bentall & The Legendary
Hearts, Keith & Kristyn Getty, Of Monsters and Men, and the Andrews Sisters
-- so the stage that decides a folder's name held the shortest list of
names it must not take apart.

curator.py's own comment had already stated the rule the code was breaking:

    "Any code that rewrites archive.artist must check here first -- the
     whole point of a guard list is that it is consulted by everything, not
     just the stage it happened to be written in."

Each entry was added because something folded it in real life: a one-off
artist pass overrode Big Brother & The Holding Company and Barney Bentall
before the damage was spotted and reversed, and a fold merged Keith &
Kristyn Getty into the 1960s solo artist "Keith" on a shared first name.

Entries are lower-cased for comparison; use `is_protected()` rather than
matching against the set directly, so the caller cannot forget to fold case.
"""

from __future__ import annotations

# The article convention is a property of this library, not of GenreLaw --
# but it must be spelled once. GenreLaw._key() already folds "The X" /
# "X, The" / "X (the)" to one form, and its docstring records what happens
# when two modules fold differently: 246 genre rules were dormant because the
# law and the library disagreed about what an artist is called. Importing it
# rather than restating it is the whole lesson of that incident. No cycle --
# genre_law imports nothing from this package -- and the import costs ~5 ms
# with no file read until a GenreLaw is constructed.
from .genre_law import GenreLaw

#: Lower-cased. Compare with is_protected(), not with `in` against a raw name.
PROTECTED_ARTIST_NAMES: frozenset[str] = frozenset(
    {
        "adam & the ants",
        "andrews sisters (the)",
        "barney bentall & the legendary hearts",
        "big brother & the holding company",
        "crosby, stills & nash",
        "crosby, stills, nash & young",
        "dr. hook & the medicine show",
        "earth, wind & fire",
        "hall & oates",
        # Unrelated to the 1960s solo artist "Keith" ("98.6") despite the
        # shared first name -- a fold merged the two before this was added.
        "keith & kristyn getty",
        # "and", not "&" -- the band's own styling, and the reason a blanket
        # ampersand rule would be wrong.
        "of monsters and men",
        "simon & garfunkel",
        "sly & the family stone",
    }
)


#: The same names under the article fold, built once at import.
_PROTECTED_KEYS: frozenset[str] = frozenset(
    GenreLaw._key(n) for n in PROTECTED_ARTIST_NAMES
)


def is_protected(name: str | None) -> bool:
    """True when this artist name must not be split, folded or re-cased.

    Folds the article convention, because otherwise the protection is
    dormant for every spelling the library actually stores.

    Measured 2026-09-08 (M-04): the canon holds "andrews sisters (the)", and
    before this fix `is_protected` lowercased and nothing else. So

        is_protected("Andrews Sisters (the)")  -> True
        is_protected("Andrews Sisters, The")   -> False
        is_protected("The Andrews Sisters")    -> False
        is_protected("Andrews Sisters")        -> False

    Only the first is a spelling this library never produces. Worse,
    `normalize.py`'s article repair rewrites names INTO the suffix form, so
    the pipeline actively converted the one working spelling into a
    non-working one before asking whether it was protected.

    A dormant rule looks exactly like an absent one -- nothing reported it,
    exactly as GenreLaw's own docstring warns.

    Note what is NOT folded: `&` stays `&`. "Of Monsters and Men" spells its
    own name with "and", and a blanket ampersand fold would merge it with a
    hypothetical "&" spelling and re-introduce the collision the canon
    comments already warn about.
    """
    return GenreLaw._key(name or "") in _PROTECTED_KEYS
