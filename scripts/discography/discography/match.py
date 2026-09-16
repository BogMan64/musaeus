"""Deciding whether an owned album tag and a MusicBrainz release group are the same record.

This is where a gap report earns or loses its credibility. A false negative --
failing to match "Darkness on the Edge of Town" to the record of the same name --
reports an album as missing that is sitting in the library. Grey goes looking for
something he owns, once, and then stops trusting the report.

So matching is deliberately generous about formatting and strict about identity.

BORROWED, NOT COPIED
--------------------
MUSAEUS's neardupe._normalise(s, strip_qualifiers=True) already folds case,
accents, punctuation, a leading "the", and version/edition brackets. That is
exactly the fold wanted here, and it is the same function fm-radio-lab borrows
for titles. Falls back to a crude local version when MUSAEUS is absent, and says
which is in force -- a fallback nobody can see is one nobody can trust.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from musaeus.brackets import CLOSE, OPEN

from .scope import ReleaseGroup

IMPLEMENTATION = "builtin"
IMPORT_ERROR: str | None = None

_musaeus_normalise = None
try:  # pragma: no cover - depends on the machine
    from musaeus.stages.neardupe import _normalise as _musaeus_normalise  # type: ignore

    IMPLEMENTATION = "musaeus"
except Exception as exc:
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    _musaeus_normalise = None


_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")
# Bracket classes from musaeus.brackets, not a private copy. Each of
# these covered parens and squares but NOT braces -- the omission that
# rule exists to catch, and invisible while this lived outside the repo.
_BRACKET_RE = re.compile(rf"[{OPEN}][^{CLOSE}]*[{CLOSE}]")

#: ALBUM edition words. Not a fork of MUSAEUS's STRIP_WORDS -- a different
#: question.
#:
#: MUSAEUS's list is tuned for TRACK version qualifiers: Live, Remix, Remaster.
#: Measured against neardupe._normalise(strip_qualifiers=True):
#:
#:     "Nebraska (2015 Remaster)"       -> "nebraska"                 matched
#:     "Nebraska [Deluxe Edition]"      -> "nebraska deluxe edition"  MISSED
#:     "Nebraska (Expanded)"            -> "nebraska expanded"        MISSED
#:     "Nebraska (Anniversary Edition)" -> "nebraska anniversary ..."  MISSED
#:
#: Those misses are false gaps -- reporting an album as missing that is in the
#: library, which is the one failure that costs the report its credibility. So
#: album editions are stripped here first and the shared fold is still delegated
#: to MUSAEUS. Adding these words to MUSAEUS's own list would be wrong: they
#: describe a packaging of a record, not a version of a performance.
_EDITION_WORDS = frozenset(
    ["deluxe", "expanded", "anniversary", "special", "collector", "collectors", "legacy", "edition", "editions", "remaster", "remastered", "remasters", "reissue", "bonus", "tracks", "track", "disc", "cd", "super", "ultimate", "limited", "digipak", "mono", "stereo", "version", "versions", "th", "st", "nd", "rd"]
)

#: Bracket contents made ENTIRELY of edition words are removed; anything else is
#: kept. This is why it is bracket-scoped rather than a bare-word strip: "Special
#: Beat Service" and "Mono" can be real album titles, and stripping those words
#: wherever they appeared would fold genuinely different records together. A
#: false match is as damaging as a false gap, in the opposite direction -- it
#: reports an album as owned when it is not.
_YEAR_OR_NUM = re.compile(r"^\d{1,4}$")


def _bracket_is_edition_only(inner: str) -> bool:
    tokens = [t for t in _PUNCT_RE.sub(" ", inner.lower()).split() if t]
    if not tokens:
        return True
    return all(t in _EDITION_WORDS or _YEAR_OR_NUM.match(t) for t in tokens)


def _strip_edition_brackets(s: str) -> str:
    """Remove bracketed segments that say only which edition this is."""
    def repl(m: re.Match[str]) -> str:
        return " " if _bracket_is_edition_only(m.group(0)[1:-1]) else m.group(0)

    out = _BRACKET_RE.sub(repl, s or "")
    # Trailing dash form: "Nebraska - Deluxe Edition". Same rule, same caution --
    # only when the tail is edition words only, so "Zuma - Live Rust" survives.
    if " - " in out:
        head, _, tail = out.rpartition(" - ")
        if head.strip() and _bracket_is_edition_only(tail):
            out = head
    return _WS_RE.sub(" ", out).strip()


#: Edition noise for the standalone fallback, which has no MUSAEUS to delegate to
#: and so must also handle the track-level words MUSAEUS would have stripped.
_FALLBACK_NOISE = (
    "remaster", "remastered", "reissue", "mono", "stereo",
    "bonus tracks", "bonus track", "live", "remix",
)
_FALLBACK_NOISE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in sorted(_FALLBACK_NOISE, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _fallback_fold(s: str) -> str:
    s = _strip_edition_brackets(s or "")
    s = _BRACKET_RE.sub(" ", s)
    s = _FALLBACK_NOISE_RE.sub(" ", s)
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    if s.startswith("the "):
        s = s[4:]
    return s


def fold(s: str) -> str:
    """An album title reduced to a comparable key.

    Album editions are stripped here, then the shared fold (case, accents,
    punctuation, leading "the", track-level version words) is delegated to
    MUSAEUS.
    """
    pre = _strip_edition_brackets(s or "")
    if _musaeus_normalise is not None:
        try:
            return _musaeus_normalise(pre, strip_qualifiers=True)
        except Exception:
            _note_runtime_failure()
    return _fallback_fold(pre)


def _note_runtime_failure() -> None:
    global IMPLEMENTATION, IMPORT_ERROR
    if IMPLEMENTATION != "builtin":
        IMPLEMENTATION = "builtin"
        IMPORT_ERROR = (
            "musaeus.stages.neardupe._normalise was imported but raised when "
            "called -- its signature has probably changed. Using the fallback."
        )


def provenance() -> str:
    if IMPLEMENTATION == "musaeus":
        return "album matching: MUSAEUS neardupe._normalise (strip_qualifiers=True)"
    return f"album matching: built-in fallback -- MUSAEUS not used ({IMPORT_ERROR})"


@dataclass(frozen=True)
class Gap:
    """A studio album the library holds nothing from."""

    artist: str
    album: str
    mbid: str
    year: int | None
    may_be_covered_by_compilation: bool = False


def owned_keys(albums: set[str]) -> set[str]:
    """Folded keys for the albums the library holds. Empty keys dropped.

    An album tag that folds to nothing -- punctuation only, or a title made
    entirely of stripped edition words -- must not become a key, because an
    empty key matches any release group that also folds to empty and would
    silently mark real gaps as owned.
    """
    return {k for k in (fold(a) for a in albums) if k}


def find_gaps(
    artist: str,
    owned: set[str],
    canonical: list[ReleaseGroup],
    has_compilation: bool = False,
) -> tuple[list[Gap], list[ReleaseGroup]]:
    """(albums owned nothing from, albums matched to something owned).

    Both halves returned: the matched list is how a reader checks the matcher
    rather than taking its word. If an artist's 18 owned albums match 2 release
    groups, the fold is broken, and that is visible only if the matches are shown.
    """
    keys = owned_keys(owned)
    gaps: list[Gap] = []
    matched: list[ReleaseGroup] = []
    for rg in canonical:
        key = fold(rg.title)
        if key and key in keys:
            matched.append(rg)
        else:
            gaps.append(
                Gap(
                    artist=artist,
                    album=rg.title,
                    mbid=rg.mbid,
                    year=rg.year,
                    may_be_covered_by_compilation=has_compilation,
                )
            )
    gaps.sort(key=lambda g: (g.year or 9999, g.album.lower()))
    return gaps, matched
