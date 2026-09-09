"""
MUSAEUS — filing names: which folder an artist's tracks live under.

Grey's rule: "when I look for a song it will be first by artist, so group
them into one folder." The tag and the folder are not always the same
question. `Louis Armstrong, Billie Holiday, Sy Oliver & His Orchestra` is
an accurate performance credit and a terrible folder name; the folder
should say `Louis Armstrong`.

WHY THIS IS A LIST AND NOT A RULE
Because every rule that works also breaks something. Measured against the
real catalogue on 2026-09-09, a "strip the backing group" pattern that
correctly turns `Bill Haley & His Comets` into `Bill Haley` also turns:

    Sly & the Family Stone  -> Sly
    Kool & The Gang         -> Kool
    Hootie & the Blowfish   -> Hootie
    Earth, Wind & Fire      -> Earth
    Peter, Paul & Mary      -> Peter

"Person plus backing group" and "band name that contains an ampersand" are
not distinguishable by pattern -- the same reason Grey's `&` versus `and`
convention had to be a ruling rather than a regex. So this is an explicit
per-artist list, and an artist absent from it is filed under their tag.
Only 119 of 2,773 artists have a shape where the question even arises;
2,654 need nothing.

NOT THE SAME FILE AS artist_canon.tsv, AND NOT THE SAME DIRECTION
    artist_canon.tsv   raw tag       -> canonical TAG    (KC -> KC & The Sunshine Band)
    artist_filing.tsv  canonical tag -> FOLDER name      (Gladys Knight & The Pips -> Gladys Knight)

artist_canon repairs a truncated tag; this chooses where the tag's tracks
are filed. Running them in the wrong order, or confusing one for the other,
would expand a tag and then file it under the fragment it was expanded
from. `check()` exists to catch exactly that.

Lives in MetaData/ with the other authorities rather than in a database
column, deliberately. Grey's rulings are all in files, so a database
rebuild loses none of them -- which is what makes a rebuild safe. A column
would put this one ruling inside the thing being rebuilt.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

FILENAME = "artist_filing.tsv"

HEADER = """\
# MUSAEUS Artist Filing — artist_tag TAB folder_name
#
# Which FOLDER an artist's tracks are filed under. An artist not listed
# here is filed under their own tag, which is the right answer for about
# 95% of the library.
#
# Add a line only where the tag is a performance credit rather than the
# name you would look under:
#
#     Gladys Knight & The Pips<TAB>Gladys Knight
#
# Do NOT add a line to shorten a real band name. `Kool & The Gang` and
# `Earth, Wind & Fire` are names, not credits, and belong in their own
# folders under their full names.
#
# Lines starting with # are ignored. Blank folder_name is an error, not
# "file under nothing".
"""


class FilingError(Exception):
    """The filing map is unusable and the caller must not guess."""


def load(meta_dir: Path) -> dict[str, str]:
    """artist tag -> folder name. Missing file is an empty map, not an error.

    An absent authority file means "no artist has been given a special
    folder", which is the correct starting state and exactly what every
    caller falls back to anyway.
    """
    path = Path(meta_dir) / FILENAME
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FilingError(f"{FILENAME} could not be read: {exc}") from exc

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" not in line:
            raise FilingError(
                f"{FILENAME} line {lineno}: expected 'artist<TAB>folder', found no tab. "
                f"A space-separated line here would silently file an artist under a "
                f"name nobody chose."
            )
        tag, folder = line.split("\t", 1)
        tag, folder = tag.strip(), folder.strip()
        if not tag or not folder:
            raise FilingError(
                f"{FILENAME} line {lineno}: both sides must be non-empty "
                f"(got {tag!r} -> {folder!r})"
            )
        if tag in out and out[tag] != folder:
            raise FilingError(
                f"{FILENAME} line {lineno}: {tag!r} is already filed under "
                f"{out[tag]!r}; a second answer of {folder!r} makes the file "
                f"ambiguous rather than overriding it."
            )
        out[tag] = folder
    return out


def folder_for(artist: str | None, filing: dict[str, str]) -> str:
    """The folder name for a tag. Absent from the map means: use the tag."""
    if not artist:
        return "Unknown Artist"
    return filing.get(artist, artist)


def check(filing: dict[str, str], canon: dict[str, str] | None = None) -> list[str]:
    """Problems worth reporting. Empty list means checked and clean.

    Distinct from returning nothing at all: a caller that cannot tell
    "I looked and found nothing" from "I did not look" will report a
    healthy library either way.
    """
    problems: list[str] = []
    for tag, folder in sorted(filing.items()):
        if folder == tag:
            problems.append(f"{tag!r} maps to itself — the line has no effect and can be removed")
        if folder in filing and filing[folder] != folder:
            problems.append(
                f"{tag!r} is filed under {folder!r}, which is itself filed under "
                f"{filing[folder]!r}; filing does not follow chains, so this "
                f"artist lands in {folder!r} and not where the second line says"
            )
        # The artist_canon collision. canon expands a truncated tag; if a
        # filing key is something canon would rewrite, the tag never
        # reaches this map under that spelling and the line is dead.
        if canon and tag in canon and canon[tag] != tag:
            problems.append(
                f"{tag!r} is rewritten by artist_canon.tsv to {canon[tag]!r} before "
                f"filing sees it, so this line never applies. File {canon[tag]!r} instead."
            )
    return problems
