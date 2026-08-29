#!/usr/bin/env python3
"""
The two forms of an artist name, and which field each one belongs in.

The problem
-----------
MUSAEUS has stored the article as a suffix -- "Stooges, The" -- since it
inherited the convention from ORPHEUS. The reason is real: "The Stooges"
sorts under T, and a library browsed by folder needs it under S.

But that string was also being written into the `artist` TAG, which is the
field every external service reads. MusicBrainz has never heard of
"Stooges, The". Measured 2026-08-29 on the live cache: 376 of 839 cached
misses were in `X, The` form, and 0 of 2,158 hits were -- not one
article-suffix lookup had ever succeeded.

The fix at query time was to flip the form before asking. This module is the
fix at rest: three fields, three jobs.

    artist (\xa9ART)   "The Stooges"    natural form -- what MusicBrainz,
                                      Plex and every player expect
    soar            "Stooges, The"   sort form -- what players sort by;
                                      this is exactly what the tag is for
    folder          "Stooges, The"   sorted browsing on disk, unchanged

The sort form does not disappear, it moves to the field that means it.

One home for the rule
---------------------
Both directions already existed, in different modules and under names that
did not say what they did. They are re-exported here rather than
reimplemented: this rule has regressed three times (the "(the)" parenthetical
form, the "Beatles, The (the)" double spelling, and De La Soul being split
into "La Soul, De"), and a fourth implementation would be a fourth chance.

    sort_form     <- normalize._move_article_to_suffix
    natural_form  <- enrich._clean_artist_for_lookup

Both are article-aware and both respect PROTECTED_ARTIST_NAMES, so
"De La Soul" and "Los Lobos" survive in either direction.
"""

from __future__ import annotations

# The two implementations live in stage modules, and stages import THIS
# module (organize builds its paths from sort_form, tagger writes both
# forms). Importing them at module scope closes that loop and Python raises
# a partially-initialized-module error on `musaeus.stages`.
#
# Imported inside the functions instead. The module cache makes the cost a
# dict lookup after the first call, and it keeps the well-tested originals
# where their tests already exercise them -- moving them would be a bigger
# change to a rule that has regressed three times.


def _to_natural(name: str) -> str:
    from .stages.enrich import _clean_artist_for_lookup

    return _clean_artist_for_lookup(name)


def _to_sort(name: str) -> str:
    from .stages.normalize import _move_article_to_suffix

    return _move_article_to_suffix(name)


# MP4 atoms. `soar`/`soaa` are the standard sort fields -- iTunes, Apple
# Music, Plex and mp3tag all honour them, and MUSAEUS has never written one.
SORT_ARTIST_ATOM = "soar"
SORT_ALBUMARTIST_ATOM = "soaa"


def natural_form(name: str) -> str:
    """The form the outside world uses. "Stooges, The" -> "The Stooges"."""
    return _to_natural((name or "").strip())


def sort_form(name: str) -> str:
    """The form that sorts. "The Stooges" -> "Stooges, The"."""
    return _to_sort((name or "").strip())


def has_article(name: str) -> bool:
    """True when the two forms differ -- i.e. the name carries an article.

    The honest test is that the transforms disagree, not a regex of our own:
    a name is article-bearing exactly when converting it changes it. That
    keeps this in step with the protected-name guards for free.
    """
    n = (name or "").strip()
    if not n:
        return False
    return natural_form(n) != sort_form(n)


def tag_values(stored_artist: str) -> dict[str, str]:
    """What the artist and sort-artist tags should hold for a stored name.

    Accepts either form -- a library mid-migration has both -- and returns
    the pair. For a name with no article both values are the same string,
    and the caller can skip writing a redundant sort tag.
    """
    n = (stored_artist or "").strip()
    if not n:
        return {}
    return {"artist": natural_form(n), "sort_artist": sort_form(n)}
