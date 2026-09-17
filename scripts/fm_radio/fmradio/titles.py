"""Reducing a title to the song, so pressings of it can be matched.

"Here I Am - Live", "Here I Am (2009 Remaster)" and "Here I Am" are one song in
three pressings. Matching them needs the version qualifier gone and nothing else.

BORROWED, NOT COPIED
--------------------
MUSAEUS already solved this in musaeus/stages/neardupe.py, where it decides
whether two files are near-duplicates. Its _normalise(s, strip_qualifiers=True)
strips version and edition brackets, folds accents, drops punctuation and removes
a leading "the". This module tries to use that and falls back to a crude local
version only when MUSAEUS is not importable, so the tool stands alone on a
machine that has no MUSAEUS but gets the better rule on Grey's.

Why the crude fallback is genuinely worse, so the difference is not theoretical:
it truncates at the first bracket, which is fine for "Here I Am (2009 Remaster)"
and destructive for "Here I Am (Just When I Thought I Was Over You)" -- where the
bracket is most of the actual title. Reduce that to "here i am" and it collides
with a different song by the same artist. MUSAEUS strips only brackets whose
contents are version words, so it keeps that one.

WHICH IMPLEMENTATION IS IN USE IS REPORTED, NOT ASSUMED
-------------------------------------------------------
The first attempt at this imported `base_title` from `musaeus.neardupe`. Both
halves were wrong: there is no base_title anywhere in MUSAEUS, and neardupe is
at musaeus.stages.neardupe. A plain try/except ImportError would have caught
that, silently used the crude version on every run forever, and reported an
upgrade that never happened. So this records what it resolved to and the CLI
prints it. A fallback you cannot see is a fallback you cannot trust.
"""

from __future__ import annotations

import re

#: Which implementation backs base_title(), for the run header.
#: One of "musaeus" or "builtin".
IMPLEMENTATION = "builtin"

#: Set when the MUSAEUS import was attempted and failed, so the reason can be
#: shown rather than guessed at.
IMPORT_ERROR: str | None = None

_musaeus_normalise = None

try:  # pragma: no cover - depends on the machine
    # The private name is imported deliberately. MUSAEUS exposes no public
    # title-normalising helper, and the alternative -- reimplementing its
    # STRIP_WORDS list and bracket rules here -- is the duplicate-rule problem
    # this module exists to avoid. Being private means it can move without
    # warning, which is exactly why the failure is visible below instead of
    # silent.
    from musaeus.stages.neardupe import _normalise as _musaeus_normalise  # type: ignore

    IMPLEMENTATION = "musaeus"
except Exception as exc:  # ImportError, but also a config load blowing up
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    _musaeus_normalise = None


_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def _builtin_base_title(t: str) -> str:
    """Fallback: truncate at the first bracket, fold case and punctuation.

    Kept crude on purpose. Making it a second, cleverer version-stripping
    implementation would recreate the divergence this module is here to prevent
    -- two rules for one question, disagreeing on the awkward cases. It is the
    obviously-worse option so that using it is obviously a downgrade.
    """
    out = (t or "").lower()
    for cut in ("(", "["):
        if cut in out:
            out = out.split(cut, 1)[0]
    out = _PUNCT_RE.sub(" ", out)
    out = _WS_RE.sub(" ", out).strip()
    if out.startswith("the "):
        out = out[4:]
    return out


def base_title(t: str) -> str:
    """A title reduced to its song, for matching one song across pressings."""
    if _musaeus_normalise is not None:
        try:
            return _musaeus_normalise(t or "", strip_qualifiers=True)
        except Exception:
            # MUSAEUS is present but its signature changed under us. Degrade
            # rather than crash a read-only report, but do it once and loudly.
            _note_runtime_failure()
    return _builtin_base_title(t)


def _note_runtime_failure() -> None:
    global IMPLEMENTATION, IMPORT_ERROR
    if IMPLEMENTATION != "builtin":
        IMPLEMENTATION = "builtin"
        IMPORT_ERROR = (
            "musaeus.stages.neardupe._normalise was imported but raised when "
            "called with (str, strip_qualifiers=True) -- its signature has "
            "probably changed. Using the crude fallback."
        )


def provenance() -> str:
    """One line for the run header saying which rule is actually in force."""
    if IMPLEMENTATION == "musaeus":
        return "title matching: MUSAEUS neardupe._normalise (strip_qualifiers=True)"
    return f"title matching: built-in crude fallback -- MUSAEUS not used ({IMPORT_ERROR})"
