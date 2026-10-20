#!/usr/bin/env python3
r"""
What counts as a bracketed annotation, defined once.

The problem
-----------
Track titles carry annotations in brackets -- "(Live)", "[2015 Remaster]",
"{Vocal Version}" -- and several places need to strip or inspect them:
NearDupe, so two pressings of one song group together; OriginalYear, so a
year in the title does not become the release year; Doctor, when it
reports on names.

Each of those grew its own regex. On 2026-09-02 there were three, and all
three were wrong in the same way:

    neardupe.py       [\(\[]\s*(?:19|20)\d{2}\s*[\)\]]
    original_year.py  \s*[\(\[][^\)\]]*[\)\]]
    doctor.py         [\(\[].*?[\)\]]

Every one knows ( ) and [ ] and none knows { }. Measured against the live
library, all three leave this untouched:

    Midnight Cruiser [In the Style of Steely Dan] {Karaoke Demonstration
    Version With Lead Vocal}

They also disagree with each other on whitespace -- doctor.py leaves a
double space where original_year.py does not -- so "the same" title
normalises differently depending on which stage looked at it.

A fourth copy was then written by hand while building the wanted-list
export, and had the identical gap. That is the tell: this is not three
people being careless, it is a shape that invites being rewritten badly.
The fix is not to correct three regexes, it is to make reaching for the
right one easier than rolling another.

What this does NOT do
---------------------
It does not decide WHICH annotations are worth stripping. NearDupe strips
only specific words ("Remaster", "Live"), deliberately, so that two
genuinely different live recordings do not collapse into one group. That
judgement stays with the caller; only the alphabet is shared.
"""

from __future__ import annotations

import re

#: Every character that opens a bracketed annotation, and every one that
#: closes it. Callers building their own narrow rule should interpolate
#: these rather than typing a character class -- that is how { } came to
#: be missing from all three earlier copies.
OPEN = r"\(\[\{"
CLOSE = r"\)\]\}"

#: One bracketed annotation, any style, non-greedy so that two annotations
#: in one title stay two matches rather than swallowing the text between.
#:
#: Deliberately does NOT require the pair to match: the live library holds
#: "Medley - Ain't That A Shame [Live}", opened square and closed curly,
#: which every earlier version left completely intact.
BRACKETED = re.compile(rf"\s*[{OPEN}][^{OPEN}{CLOSE}]*[{CLOSE}]")

_WS = re.compile(r"\s{2,}")


def strip_bracketed(text: str) -> str:
    """Remove every bracketed annotation and leave the spacing sane.

    >>> strip_bracketed("I Can't Go for that (No Can Do) {Vocal Version}")
    "I Can't Go for that"
    >>> strip_bracketed("Medley [Live}")
    'Medley'

    Whitespace is collapsed here rather than left to the caller, because
    the two earlier implementations differed on exactly that and produced
    titles that compared unequal for no visible reason.
    """
    if not text:
        return ""
    return _WS.sub(" ", BRACKETED.sub("", text)).strip()


def has_bracketed(text: str) -> bool:
    """True when *text* carries at least one bracketed annotation."""
    return bool(text) and BRACKETED.search(text) is not None


#: A " - " ending that names a version of the same recording: "Slow Ride -
#: 2016 Remaster", "Layla - Acoustic Live". A short run of words may come
#: before the marker. Not "Part 2": that is another piece (Codex review of
#: #209 -- AcoustID took "Song - Part 1" for "Song - Part 2"). Doctor's rule
#: since the possible-duplicates report; one copy now, here.
EDITION_SUFFIX = re.compile(
    # A part before the words is kept, the version after it is not: "Song -
    # Part 1 Live" is "Song - Part 1" (Codex review of #216, two rounds).
    r"\s*-\s*(?P<part>(?:part|pt)\.?\s*(?:\d+|[ivxlc]+)\s+)?"
    r"(?:(?!(?:part|pt)\b)\w+\s+){0,2}"
    r"(remaster|remastered|live|mono|stereo|single|album|radio|rerecorded|version|mix|edit)\b.*$",
    re.IGNORECASE,
)


_PART_WORDS = {
    w: str(n)
    for n, w in enumerate(
        [
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
        ],
        1,
    )
}
_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}

#: "Part 2", "Pt. 3", "Part IV", "Part Two": which piece of a work a title names.
_PART = re.compile(
    r"\b(?:part|pt)\.?\s*(\d+|[ivxlc]+|" + "|".join(_PART_WORDS) + r")\b", re.IGNORECASE
)


def _part_number(label: str) -> str:
    """'2', 'II', 'two' -> '2'."""
    label = label.lower()
    if label.isdigit():
        return str(int(label))
    if label in _PART_WORDS:
        return _PART_WORDS[label]
    total = 0
    for here, after in zip(label, [*label[1:], ""], strict=True):
        value = _ROMAN[here]
        total += -value if after and _ROMAN[after] > value else value
    return str(total)


def same_part_spelling(text: str) -> str:
    """Every part label written one way: "Song - Pt. II" -> "Song - Part 2"."""
    return _PART.sub(lambda m: f"Part {_part_number(m.group(1))}", text or "")


def part_numbers(text: str) -> tuple[str, ...]:
    """The parts a title names, as numbers: ("1",) for "Song - Part One"
    (Codex review of #216: "Part I" is "Part 1", "Part One" is a part)."""
    return tuple(_part_number(m) for m in _PART.findall(text or ""))


def base_title(text: str) -> str:
    """A title without its version: brackets and a version " - " ending set
    aside, a part kept ("Song - Part 1 Live" -> "Song - Part 1"), also from
    brackets ("Song (Part 2)" -> "Song - Part 2", review of #216)."""
    title = strip_bracketed(text or "")
    m = EDITION_SUFFIX.search(title)
    if m is not None:
        part = m.group("part")
        title = title[: m.start()] + (f" - {part.strip()}" if part else "")
    kept = part_numbers(title)
    return title + "".join(f" - Part {n}" for n in part_numbers(text) if n not in kept)
