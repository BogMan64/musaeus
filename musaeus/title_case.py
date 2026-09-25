"""One ruling on how an album title is capitalised.

Grey's call, 2026-09-16: "can we set a standard on At vs at, i like at."

The library had the same album filed twice under two casings -- "Back in
Black" beside "Back In Black", 28 such pairs per tier -- which on a
folder-browsed library is two albums, and in the car is two entries in the
list. Merging them needs an answer to "which spelling is right", and that
answer has to live in ONE place or the next caller invents a fourth.

THE RULE

Standard English title case: capitalise everything except articles, short
coordinating conjunctions and short prepositions, and capitalise those anyway
when they open or close the title, or follow punctuation that starts a new
phrase.

    Back in Black                 not  Back In Black
    Highway to Hell               not  Highway To Hell
    Eat to the Beat               not  Eat To the Beat
    The Best Of                   ("Of" is last -- it stays capitalised)

WHAT IT REFUSES TO TOUCH, AND WHY

Blind .title() is what produced half this mess. Three kinds of word must
survive exactly as written:

  acronyms and initialisms   U.S.A, R.E.M., ABBA, CCR. "Born in the U.S.A"
                             must not become "Born In The U.s.a", which is
                             the failure the normalize stage already carries
                             a PROTECTED_ARTIST_CASING list for.

  interior capitals          McCartney, DeBarge, MacArthur, O'Brien. A
                             lowercase-the-rest rule turns these into
                             Mccartney, which is wrong and looks careless.

  anything with a digit      "Vol. 6", "1964", "Blink-182". Case rules have
                             no opinion about these and applying one only
                             creates opportunities to be wrong.

`so` is deliberately NOT in the minor-word list. It is a coordinating
conjunction in "and so to bed" and an adverb in "She's So Unusual", and no
rule available here can tell those apart -- so it keeps its capital, which is
right in the case the library actually contains.
"""

from __future__ import annotations

import re

#: Lowercased inside a title, capitalised at either end. Articles, the short
#: coordinating conjunctions, and prepositions of four letters or fewer --
#: the usual line, drawn where most house styles draw it.
MINOR_WORDS = frozenset(
    # articles
    ("a", "an", "the")
    # coordinating conjunctions ("so" is deliberately absent -- see below)
    + ("and", "but", "or", "nor", "for", "yet")
    # prepositions of four letters or fewer
    + ("as", "at", "by", "in", "of", "on", "to", "up", "via")
    + ("from", "into", "onto", "over", "with", "off", "out", "per")
)

#: A new phrase starts after these, so the next word is capitalised even when
#: it is a minor word: "Live 1964 - At Philharmonic Hall".
_PHRASE_BREAK = re.compile(r"[:;.!?–—]$|^-$")

#: Splits on whitespace and hyphens while KEEPING them, so the original
#: spacing is reproduced exactly rather than normalised on the way through.
_TOKENS = re.compile(r"(\s+|-)")


def _is_fixed(word: str) -> bool:
    """True for a word whose capitalisation must be left exactly as found."""
    core = word.strip("(){}[]\"'‘’“”,.!?")
    if not core:
        return True
    if any(ch.isdigit() for ch in core):
        return True
    letters = [c for c in core if c.isalpha()]
    if not letters:
        return True
    # ALL CAPS of more than one letter: an acronym, or a band that shouts.
    if len(letters) > 1 and all(c.isupper() for c in letters):
        return True
    # A capital anywhere but the front: McCartney, DeBarge, iPod.
    if any(c.isupper() for c in core[1:]):
        return True
    # Dotted initialism, even in lower case: r.e.m.
    return core.count(".") >= 2


def _cap(word: str) -> str:
    """Capitalise the first letter, leaving the rest of the word alone."""
    for i, ch in enumerate(word):
        if ch.isalpha():
            return word[:i] + ch.upper() + word[i + 1 :]
    return word


def _lower(word: str) -> str:
    return word.lower()


def album_title_case(text: str) -> str:
    """Return *text* in the house style. Idempotent."""
    if not text or not text.strip():
        return text

    parts = _TOKENS.split(text)
    word_idx = [i for i, p in enumerate(parts) if p.strip() and p != "-"]
    if not word_idx:
        return text

    first, last = word_idx[0], word_idx[-1]
    out = list(parts)

    for pos, i in enumerate(word_idx):
        w = parts[i]
        if _is_fixed(w):
            continue
        # A word is "opening" if it is first, last, or follows a phrase break.
        opening = i in (first, last)
        if not opening and pos > 0:
            prev = parts[word_idx[pos - 1]]
            if _PHRASE_BREAK.search(prev.strip()):
                opening = True
            # a hyphen between them also starts a phrase: "Live 1964 - At ..."
            between = "".join(parts[word_idx[pos - 1] + 1 : i])
            if "-" in between:
                opening = True
        stripped = w.strip("(){}[]\"'‘’“”")
        if not opening and stripped.lower() in MINOR_WORDS:
            out[i] = _lower(w)
        else:
            out[i] = _cap(w)

    return "".join(out)


# ── Artist names and song titles: every word capitalised ─────────────────────
#
# Grey, 2026-09-25, after finding "Dead Or Alive" and "Dead or Alive" as two
# folders: "the simpler solution ... every word with a few exceptions
# capitalised" -- for artist names and song titles. He was told that album
# names follow album_title_case above ("i like at", 2026-09-16) and chose this
# for artists and titles anyway, so albums are deliberately not covered here.
#
# It only ever RAISES a first letter. Nothing is lowercased, so every spelling
# someone chose on purpose survives: ABBA, AC/DC, McCartney, eBay (_is_fixed).
# The agreed exceptions:
#
#   after an apostrophe   "Don't", never "Don'T"; and a word that STARTS with
#                         one ('n', 'til, 'em) is left alone entirely
#   feat. and vs.         join two credits; not words of either name

#: Left exactly as written: they join two credits rather than belong to one.
KEEP_AS_WRITTEN = frozenset({"feat.", "feat", "ft.", "ft", "vs.", "vs"})

_WHITESPACE = re.compile(r"(\s+)")


def _every_word_cap(word: str) -> str:
    core = word.lstrip('([{"“')
    if not core or core[0] in "'‘’`":
        return word
    if core.lower().rstrip(",)]}") in KEEP_AS_WRITTEN:
        return word
    if _is_fixed(word):
        return word
    return _cap(word)


def every_word_capitalised(text: str) -> str:
    """Return *text* with every word starting in a capital. Idempotent.

    For artist names and song titles only (see the note above). Splits on
    whitespace, so "Bachman-Turner" and "Rock 'n' Roll" keep their inner
    spelling, and the original spacing is reproduced exactly.
    """
    if not text or not text.strip():
        return text
    return "".join(p if not p.strip() else _every_word_cap(p) for p in _WHITESPACE.split(text))
