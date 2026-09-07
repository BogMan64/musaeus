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
