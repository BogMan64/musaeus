#!/usr/bin/env python3
"""
MUSAEUS — Stage: NearDupe
Metadata-based near-duplicate detection (no audio fingerprinting required).

What it does:
  - Loads all CATALOGUED archive rows
  - Groups tracks by normalised artist name (ArtistCanon → fuzzy ≥88)
  - Within each artist group, strips version/edition qualifiers from
    titles (Remaster, Live, Remix, Acoustic, etc. — ORPHEUS-compatible,
    see STRIP_WORDS below) before comparing normalised title pairs with
    rapidfuzz fuzz.ratio — flags pairs scoring ≥ TITLE_THRESHOLD (88)
  - Guards against merging two different live recordings of the same
    song into one group (both sides carry a live marker in the raw,
    pre-strip title → skipped, since "live" is stripped before scoring
    and would otherwise make them look identical)
  - Stages flagged pairs in the duplicates table with type='NEAR'
  - Skips pairs already in one EXACT group together
  - dry_run() reports matches without writing to DB
  - Re-run safe: INSERT OR IGNORE on (group_id, file_path)

Design decisions (ported from ORPHEUS's SCRIPTS/lib/orpheus_fuzzy.py):
  - Artist normalisation: lowercase, strip punctuation, collapse whitespace
  - Title normalisation: strip version/edition brackets and bare
    STRIP_WORDS, then same as artist + strip leading "the "
  - group_id: "near_{sha8}" where sha8 is SHA-256[:8] of sorted file paths
  - Confidence: the fuzz.ratio score (0.0–1.0)
  - O(n²) within artist groups — fast because groups are small (< 200 tracks)
  - Threshold is conservative at 88 to avoid false positives; the
    stripping (not the threshold) is what catches "Yesterday" vs
    "Yesterday (Remaster)" and similar version-variant pairs — a bare
    fuzz.ratio on unstripped titles scores well below 88 for these
    (e.g. "Yesterday" vs "Yesterday (Remaster)" = 66.7)

The resulting near-duplicate groups appear in `musaeus dedupe` alongside
exact duplicates.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from pathlib import Path

try:
    from rapidfuzz import fuzz

    _HAVE_RAPIDFUZZ = True
except ImportError:
    _HAVE_RAPIDFUZZ = False

from ..brackets import BRACKETED, CLOSE, OPEN
from ..canon import ArtistCanon
from ..context import RunContext, StageResult
from .base import BaseStage, StageError
from .dupe_resolver import _is_live

logger = logging.getLogger(__name__)

# Minimum fuzz.ratio to flag as near-duplicate (0–100 scale)
TITLE_THRESHOLD = 88
ARTIST_THRESHOLD = 88

# Version/edition words stripped from titles before fuzzy comparison.
# ORPHEUS-compatible (SCRIPTS/lib/orpheus_fuzzy.py STRIP_WORDS).
STRIP_WORDS: tuple[str, ...] = (
    "remastered",
    "remaster",
    "remix",
    "live",
    "acoustic",
    "radio edit",
    "single version",
    "album version",
    # Sam & Dave "Hold On I'm Comin' (Original Mix)" was never paired with
    # "Hold On, I'm Comin'" by name, only by fingerprint (Odds & Ends, 2026-10-23).
    "original mix",
    "mono",
    "stereo",
    "demo",
    "karaoke",
)

# Brackets containing ONLY version words (+ optional year/digits/punctuation)
# are safe to strip. Deliberately NOT stripping all bracketed content —
# arbitrary parentheticals can be part of the real title (e.g. "Here I Am
# (Come and Take Me)" must not collapse to "Here I Am").
#: Version labels only as a label -- a whole bracket, or after " - " at the
#: end -- never as a word of the title: "Original Sin" is not "Sin". The first
#: real batch (2026-10-23): David Bowie "Dancing In The Street (Edit)" and Jerry
#: Lee Lewis "Mean Woman Blues - Original" were their pairs' recordings by
#: fingerprint, but not by name.
LABEL_WORDS: tuple[str, ...] = (*STRIP_WORDS, "edit", "original")
_LABELS = r"|".join(re.escape(w) for w in sorted(LABEL_WORDS, key=len, reverse=True))

_VERSION_BRACKET_WORDS = re.compile(
    rf"[{OPEN}]\s*(?:(?:19|20)\d{{2}}\s+)?(?:{_LABELS})[\s\d,./+-]*[{CLOSE}]",
    re.IGNORECASE,
)
#: The same label as a " - " suffix: "Song - 2011 Remaster", "Song - Original".
VERSION_SUFFIX = re.compile(
    rf"\s+-\s+(?:(?:19|20)\d{{2}}\s+)?(?:{_LABELS})(?:\s+(?:19|20)\d{{2}})?\s*$",
    re.IGNORECASE,
)
# Bracket characters come from brackets.py so this file cannot drift
# from the others again -- all three earlier copies knew ( ) and [ ]
# and none knew { }. Only the ALPHABET is shared: WHICH annotations are
# safe to strip stays here, deliberately, per the comment above.
_YEAR_BRACKET_RE = re.compile(rf"[{OPEN}]\s*(?:19|20)\d{{2}}\s*[{CLOSE}]")  # "(2015)"
_NUM_BRACKET_RE = re.compile(rf"[{OPEN}]\s*\d{{1,2}}\s*[{CLOSE}]")  # "[2]"

# Same words also stripped as bare whole-words (catches "Song Title Remix"
# with no surrounding parens at all).
_STRIP_WORDS_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in sorted(STRIP_WORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

# Raw (pre-strip) live-recording marker, used to avoid merging two
# different live recordings of the same song into one near-dupe group.
_LIVE_MARKER_RE = re.compile(r"\b(live|in concert)\b", re.IGNORECASE)


def _has_live_marker(raw_title: str) -> bool:
    """True if the raw (unstripped) title looks like a live recording."""
    return bool(_LIVE_MARKER_RE.search(raw_title))


# Two copies this close in length, under the same title, are one performance.
# The baked copies and their originals measured 0.0-0.1 s apart; two nights of
# one song are seconds apart. dupeGuru found 10 live pairs (Gary Moore, The
# Police, Cream ...) this guard kept apart (Grey, 2026-10-05): the titles
# differed only by "-" against "/" (the file name could not hold "/"), and one
# pair was 1.3 s apart. The length tolerance is the keep rule's own, 2 s.
_SAME_TAKE_SECONDS = 2.0


def _same_take(a: dict, b: dict) -> bool:
    """True if two live-marked copies are the same performance, not two.

    The live guard kept every pair of live titles apart. A baked copy and its
    own original are one concert, so 32 baked live copies stayed filed beside
    their originals (2026-09-26). Same title (punctuation and case ignored)
    AND the same length.
    """
    same_title = _normalise(a["title"]) == _normalise(b["title"])
    da, db = a.get("duration"), b.get("duration")
    return same_title and da is not None and db is not None and abs(da - db) <= _SAME_TAKE_SECONDS


# Classical works carry TWO independent identifiers, and conflating them
# was my first mistake here: a work number ("No. 1", "Op. 12", "RV 317",
# "BWV 974") says WHICH PIECE, and a movement marker ("I.", "II.",
# "Movement 3", "Act 2") says WHICH PART OF IT. Two tracks differ if
# either differs.
#
# The first version searched for whichever came first and returned "1" for
# "Violin Concerto No. 1 ... - I. Allegro" -- the work number -- so
# comparing it to "... - II. Adagio" also yielded "1" and the pair stayed
# grouped. Exactly the case the guard existed for.
_WORK_RE = re.compile(r"\b(?:No\.|Op\.|BWV|HWV|RV|K\.|D\.|Hob\.)\s*(\d+[a-z]?)", re.IGNORECASE)
_MOVEMENT_RE = re.compile(
    r"(?:^|[\s\-–—:,(\[])(?:([IVXLC]{1,5})\.(?=\s)|"
    r"(?:Movement|Mvt\.?|Part|Act|Scene)\s*(\d+|[IVXLC]{1,5})\b)",
    re.IGNORECASE,
)


def _classical_id(raw_title: str) -> tuple[str | None, str | None]:
    """(work, movement) identifiers found in a title, either may be None."""
    t = raw_title or ""
    w = _WORK_RE.search(t)
    m = _MOVEMENT_RE.search(t)
    work = w.group(1).lower() if w else None
    mov = next((g for g in (m.groups() if m else ()) if g), None)
    return work, (mov.lower() if mov else None)


#: A catalogue number names one work of one composer: "BWV 1068", "RV 293",
#: "K. 550" and "KV 550" alike. "No." and "Op." do not -- every composer has a
#: "Symphony No. 5".
_CATALOGUE_RE = re.compile(
    r"\b(BWV|HWV|RV|KV|K\.|D\.|Hob\.)\.?\s*(\d+[a-z]?(?:(?:\s*/\s*|-)\d+[a-z]?)*)", re.IGNORECASE
)
#: An arrangement is the same piece (Grey, 2026-10-26: "does an arrangement
#: count as the same piece? = yes"). A medley, or a jazz improvisation on the
#: piece (Bobby McFerrin's "Song For Amadeus"), is not.
_NOT_THE_PIECE_RE = re.compile(r"\bimprovis|\bmedley", re.IGNORECASE)
#: A part of a movement is not the movement: an "Air (Excerpt)" or "(Radio Edit)"
#: never takes the shortcut to the full Air, at any length (Codex review of #255).
_PART_OF_RE = re.compile(
    r"\bexcerpt|\bfragment|\bedit(?:ed)?\b|\babridged|\bshortened|\b(?:short|radio|single) version",
    re.IGNORECASE,
)
#: The sections of a Mass, each restarting its numbering at "I.": "Kyrie: I."
#: and "Gloria: I." are two pieces (Codex review of #255).
_MASS_SECTION_RE = re.compile(r"\b(kyrie|christe|gloria|credo|sanctus|benedictus|agnus)\b", re.I)
#: A vocal work or one of its sections. Masses, Requiems, cantatas and the like
#: restart their numbering in every section, and a title cannot always say
#: which ("Offertorium: I." and "Sequentia: I."; Codex review of #255): the
#: shortcut is for instrumental works, numbered once per work.
_VOCAL_WORK_RE = re.compile(
    r"\b(?:mass|messe|missa|requiem|cantata|kantate|oratorio|oratorium|passion|vespers?"
    r"|magnificat|opera|motet|motette|anthem|te deum|stabat|psalm|introitus|kyrie|christe"
    r"|gloria|credo|sanctus|benedictus|agnus|sequentia|offertorium|communio|lacrimosa"
    r"|chorus|choral|aria|recitative|recitativo)\b",
    re.IGNORECASE,
)
#: "No. 1" and "No. 3" of one catalogue number are two items of one work.
_ITEM_RE = re.compile(r"\bNo\.\s*(\d+)", re.IGNORECASE)
#: Words no movement is named by: articles, and how a copy was made ("- Live",
#: "Remastered") -- a shared "Live" paired two Mass sections (Codex review of #255).
_NAME_STOP = frozenset(
    {"the", "and", "for", "von", "der", "die", "das", "und", "del", "les", "des"}
    | {"live", "remaster", "remastered", "version", "edit", "mono", "stereo", "mix", "remix"}
    | {"bonus", "demo", "take", "recording", "recorded", "concert", "session", "excerpt"}
    # Placeholders and arrangement labels name no movement: "[Without
    # Indication]", "(Arr. For Piano)".
    | {"without", "indication", "arr", "arranged", "arrangement", "trans", "transcription"}
    | {"transcribed", "adaptation", "bearbeitung", "reduction", "orchestration", "solo"}
    | {"piano", "violin", "cello", "guitar", "flute", "organ", "harpsichord", "orchestra"}
    | {"ensemble", "recorder", "hands", "four", "two", "voices", "voice", "strings"}
)
#: A key names no movement: "Prelude In C Major", "Fugue In C Major" (CodeRabbit, #255).
_KEY_WORDS = frozenset(
    {"major", "minor", "dur", "moll", "sharp", "flat", "majeur", "mineur"}
    | {"maggiore", "minore", "mayor", "menor"}
)
#: Tempo and its qualifiers: a shared "Allegro" alone joins no two names.
_TEMPO_WORDS = frozenset(
    {"grave", "largo", "larghetto", "lento", "adagio", "adagietto", "andante", "andantino"}
    | {"moderato", "allegretto", "allegro", "vivace", "vivo", "presto", "prestissimo"}
    | {"maestoso", "sostenuto", "cantabile", "molto", "assai", "poco", "meno", "piu", "non"}
    | {"troppo", "con", "brio", "moto", "tempo"}
)


def _catalogues(title: str) -> list[tuple[str, str]]:
    """Every catalogue number in a title, in order, once: one track can hold
    two works ("BWV 1068 - Air / BWV 1067 - Badinerie"; Codex review of #255),
    and a title written out twice names its work twice."""
    found = (
        (
            m.group(1).lower().rstrip(".").replace("kv", "k"),
            re.sub(r"\s", "", m.group(2).lower()).replace("-", "/"),
        )
        for m in _CATALOGUE_RE.finditer(title or "")
    )
    return list(dict.fromkeys(found))


def _items(title: str) -> list[str]:
    return list(dict.fromkeys(m.group(1).lstrip("0") for m in _ITEM_RE.finditer(title or "")))


#: An arrangement label, to its closing bracket or the title's end: "(Arr. Myra
#: Hess For Piano)" names the arranger, not the movement (Codex review of #255).
_ARRANGEMENT_LABEL_RE = re.compile(
    r"\b(?:arr|arranged|arrangement|trans|transcription|transcribed|adaptation|bearbeitung"
    rf"|reduction|orchestration)\b[^{CLOSE}]*",
    re.IGNORECASE,
)


#: The parts of a title after its movement marker: "Prelude - Live In Berlin".
_NAME_PART_RE = re.compile(r"\s[-\u2013\u2014]\s|[:;/]")
#: A part holding one of these is a note on the recording, not a name.
_RECORDING_NOTE = frozenset(
    {"live", "concert", "recorded", "recording", "session", "remaster", "remastered"}
    | {"version", "edit", "mix", "remix", "mono", "stereo", "bonus", "demo", "take"}
)


def _movement_name(title: str) -> set[str]:
    """The words of a movement's own name, after its last marker: "II. Air On
    The G String (Arr. For Piano)" -> {"air", "string"}."""
    marks = list(_MOVEMENT_RE.finditer(title or ""))
    if not marks:
        return set()
    # A bracket never names it: "(Live In Berlin)" is no movement, and "II. (Air)"
    # counts as unnamed, so it takes no shortcut (Codex review of #255).
    tail = BRACKETED.sub(" ", _ARRANGEMENT_LABEL_RE.sub(" ", title[marks[-1].end() :]))
    tail = _CATALOGUE_RE.sub(" ", tail)  # "Air, BWV 1068": "BWV" is no name
    plain = "".join(
        ch for ch in unicodedata.normalize("NFD", tail.casefold()) if not unicodedata.combining(ch)
    )
    # A second name across "/" or ";", "II. Air / Gavotte", is a second movement,
    # so the track counts as unnamed; " - " and ":" join one movement's tempos,
    # "Allegro Non Molto - Allegro" (Codex review of #255).
    # A key still counts here: "Andantino / Minore" is two sections (CodeRabbit, #255).
    if sum(bool(_first_name(p, _NAME_STOP)) for p in re.split(r"[/;]", plain)) > 1:
        return set()
    return _first_name(plain, _NAME_STOP | _KEY_WORDS)


def _first_name(text: str, stop: frozenset[str]) -> set[str]:
    """The first part that is not a note on the recording: "prelude - live in
    berlin" is only {"prelude"} (Codex review of #255)."""
    for part in _NAME_PART_RE.split(text):
        words = re.findall(r"[^\W\d_]+", part)
        name = {w for w in words if len(w) > 2 and w not in stop}
        if name and _RECORDING_NOTE.isdisjoint(words):
            return name
    return set()


def _is_different_piece(title_a: str, title_b: str) -> bool:
    """True when two titles name different works, or different movements.

    Conservative on purpose: a difference is only asserted when BOTH sides
    carry the identifier being compared. One side having a movement marker
    and the other not is the ordinary "Yesterday" vs "Yesterday (Live)"
    case, which the existing stripping already handles. A catalogue number on
    both sides outranks the first identifier: "Concerto No. 3, RV 293" and
    "RV 293, Op. 8 No. 3" are one work.
    """
    wa, _ = _classical_id(title_a)
    wb, _ = _classical_id(title_b)
    ca, cb = _catalogues(title_a), _catalogues(title_b)
    if ca and cb:
        # "No. 1" and "No. 3" of BWV 232 are two items (Codex review of #255).
        na, nb = _items(title_a), _items(title_b)
        if ca != cb or (na and nb and na != nb):
            return True
    elif wa and wb and wa != wb:
        return True
    sa = {m.lower() for m in _MASS_SECTION_RE.findall(title_a or "")}
    sb = {m.lower() for m in _MASS_SECTION_RE.findall(title_b or "")}
    if sa and sb and sa != sb:
        return True
    # Every marker, not only the first: "Part 1 - I." and "Part 1 - II." differ
    # (Codex review of #255), and so does "VIII. Hornpipe" from "VII. Bourrée -
    # VIII. Hornpipe", one track holding two movements.
    ma, mb = _movements(title_a), _movements(title_b)
    return bool(ma and mb and ma != mb)


_MARKER_KIND = re.compile(r"movement|mvt|part|act|scene", re.IGNORECASE)
_ROMAN_VALUE = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}


def _marker_number(marker: str) -> int:
    """ "II", "ii" and "2" are one number (Codex review of #255)."""
    if marker.isdigit():
        return int(marker)
    values = [_ROMAN_VALUE[ch] for ch in marker.lower()]
    return sum(-v if v < nxt else v for v, nxt in zip(values, [*values[1:], 0], strict=True))


def _movement_markers(title: str) -> list[tuple[str, int]]:
    """Every movement marker in a title as (kind, number), in order: "Part 1 ...
    - II." is [("part", 1), ("movement", 2)], never just its first (Codex review
    of #255). "II." and "Movement 2" are one movement; "Act 2" is not, an act
    holding many pieces."""
    found = []
    for m in _MOVEMENT_RE.finditer(title or ""):
        roman, number = m.groups()
        word = None if roman else _MARKER_KIND.search(m.group(0))
        kind = word.group(0).lower() if word else "movement"
        found.append(("movement" if kind == "mvt" else kind, _marker_number(roman or number)))
    return found


def _movements(title: str) -> list[tuple[str, int]]:
    """The movement markers of a title, each once."""
    return list(dict.fromkeys(_movement_markers(title)))


def _same_piece(title_a: str, title_b: str) -> bool:
    """One movement of one work in two wordings: the same catalogue number and
    the same movement markers, all written on both sides (Grey, 2026-10-24:
    one copy per classical piece)."""
    if any(
        rx.search(title or "")
        for rx in (_NOT_THE_PIECE_RE, _VOCAL_WORK_RE, _PART_OF_RE)
        for title in (title_a, title_b)
    ):
        return False
    ca = _catalogues(title_a)
    ma = _movements(title_a)
    # One movement itself, not only an act, scene or part: two arias of "Act 1"
    # are two pieces. A track of two movements is named after its last one
    # only, so "I. Prelude / II. Fugue" -- or "/ I. Fugue" -- never takes the
    # shortcut (Codex review of #255).
    movement = all(
        sum(kind == "movement" for kind, _ in _movement_markers(t)) == 1 for t in (title_a, title_b)
    )
    if not (ca and ca == _catalogues(title_b) and movement and ma == _movements(title_b)):
        return False
    # Both movements named, sharing a word of the name: a Mass restarts at "I."
    # in every section, and "Kyrie eleison" is not "Gloria in excelsis". An
    # unnamed one ("I. [Without Indication]") would pair with every name and
    # join two different pieces through itself (Codex review of #255).
    na, nb = _movement_name(title_a), _movement_name(title_b)
    # Both named beyond a tempo: share that name, "Prelude Allegro" is not
    # "Fugue Allegro"; "Largo" alone still meets "Largo E Pianissimo" (CodeRabbit, #255).
    if na - _TEMPO_WORDS and nb - _TEMPO_WORDS:
        na, nb = na - _TEMPO_WORDS, nb - _TEMPO_WORDS
    return bool(na & nb)


def _strip_title_qualifiers(s: str) -> str:
    """Remove version/edition brackets and suffixes, and bare STRIP_WORDS, from a title."""
    # Brackets first: "Song - Original (Live)" ends in its suffix only once the
    # bracket is gone (Codex review of #232, round 3).
    s = _VERSION_BRACKET_WORDS.sub(" ", s)
    s = VERSION_SUFFIX.sub("", s)
    s = _YEAR_BRACKET_RE.sub(" ", s)
    s = _NUM_BRACKET_RE.sub(" ", s)
    s = _STRIP_WORDS_RE.sub(" ", s)
    return s


_AMPERSAND = re.compile(r"\s*&\s*")

#: A bracket that only names who played with whom: "(feat. Rob Thomas)",
#: "(With Paul McCartney)". The same recording is filed with and without it.
#: "feat." needs its dot: "(Feat Of Strength)" is a subtitle, not a guest.
_CREDIT_BRACKET = re.compile(
    rf"[{OPEN}]\s*(?:with|feat\.|featuring|ft\.?)\s[^{CLOSE}]*[{CLOSE}]", re.I
)


def _credit(title: str) -> str:
    """The guest a title's credit bracket names, folded; "" if none."""
    m = _CREDIT_BRACKET.search(title or "")
    return re.sub(r"\W+", " ", m.group(0).lower()).strip() if m else ""


def _normalise(s: str, strip_qualifiers: bool = False, fold_and: bool = True) -> str:
    """
    Lowercase, NFD-normalise, strip punctuation, collapse whitespace.

    strip_qualifiers=True additionally removes version/edition words
    (Remaster, Live, Remix, etc.) — used for titles, not artist names.
    """
    if strip_qualifiers:
        s = _strip_title_qualifiers(s)
    # "&" and "and" are one word (Grey, 2026-10-12): "Jack & Diane" and "Jack
    # And Diane", "Shout (Part 1 & 2)" and "(Part 1 And 2)" are one song, and
    # stripping "&" as punctuation scored them under the threshold.
    if fold_and:
        s = _AMPERSAND.sub(" and ", s)
    s = unicodedata.normalize("NFD", s.lower())
    s = re.sub(r"[^\w\s]", " ", s)  # punctuation → space
    s = re.sub(r"\s+", " ", s).strip()  # collapse whitespace
    # Strip leading "the "
    if s.startswith("the "):
        s = s[4:]
    return s


# A credit that names a second artist after the first: "Stevie Ray Vaughan &
# Double Trouble", "Janis Joplin, Big Brother ...", "Duke Ellington & His
# Orchestra". dupeGuru found 57 pairs of one recording filed under the plain
# name and under the credit with a collaborator, which the artist buckets
# never compared (Grey, 2026-10-05).
_COLLAB_SPLIT_RE = re.compile(
    r"\s*(?:,|&|/|\band\b|\bwith\b|\bfeat\.|\bfeaturing\b)\s*", re.IGNORECASE
)

# A credit with a collaborator is only paired with the plain name when the two
# copies are the same length too: "X" and "X & Y" are often two recordings.
_COLLAB_SECONDS = 3.0


#: A band named after its leader: "The Jimi Hendrix Experience" is Jimi Hendrix's
#: act (Grey, 2026-10-23 -- his songs arrived as "Jimi Hendrix" twice and were
#: never compared with the library's "The Jimi Hendrix Experience").
_BAND_OF = re.compile(
    r"^(?:the\s+)?(.+?)\s+(?:experience|band|orchestra|group|trio|quartet)$", re.I
)


def _primary_artist_key(raw_artist: str) -> str:
    """The normalised first artist of a credit, or the leader a band is named
    after ("" if it names only one act)."""
    parts = _COLLAB_SPLIT_RE.split(raw_artist.strip(), maxsplit=1)
    if len(parts) >= 2 and parts[0].strip():
        return _normalise(parts[0])
    m = _BAND_OF.match(raw_artist.strip())
    # The leader's name keeps two words or more: "The Band", "The Who Band"
    # name no leader.
    return _normalise(m.group(1)) if m and len(m.group(1).split()) >= 2 else ""


def _without_artist_prefix(norm_title: str, norm_artist: str) -> str:
    """The title with the artist's own name taken off the front, for COMPARISON only.

    The wanted list and the library both held titles like "Toto Africa" under the
    artist Toto, "Kinks Lola", "Belinda Carlisle I Feel Free" (Grey, 2026-10-05: 64
    in the library, 14 with the clean-titled song beside them). A fuzzy ratio of
    "toto africa" against "africa" is about 70, far under the threshold, so such a
    pair was never staged. Both arguments are already normalised. Nothing is left
    when the title IS the artist's name, and then the title stays whole. This is
    only ever one MORE way to compare two titles (the best score wins); nothing is
    rewritten, so a title that merely starts with a band's word ("Heart Of Glass")
    can at worst be offered for a person to judge.
    """
    if len(norm_artist) >= 3 and norm_title.startswith(norm_artist + " "):
        rest = norm_title[len(norm_artist) + 1 :].strip()
        if rest:
            return rest
    return norm_title


def _group_id(path_a: str, path_b: str) -> str:
    """Stable group ID from the two sorted paths."""
    combined = "\n".join(sorted([path_a, path_b]))
    return "near_" + hashlib.sha256(combined.encode()).hexdigest()[:8]


class NearDupeStage(BaseStage):
    """
    Near-duplicate detection — find tracks with the same artist and very
    similar titles, staging them for review in the dedupe console.
    """

    NAME = "neardupe"

    # ── Validate ──────────────────────────────────────────────────────────────

    def validate(self, ctx: RunContext) -> None:
        if not _HAVE_RAPIDFUZZ:
            raise StageError(
                "rapidfuzz not installed — required for near-duplicate detection.\n"
                "Install with: pip install rapidfuzz"
            )
        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'"
        ).fetchone()[0]
        logger.info("[neardupe] %d catalogued tracks to compare", count)

    # ── Shared logic ──────────────────────────────────────────────────────────

    def _detect(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)

        cfg = ctx.config
        artist_canon = ArtistCanon(cfg.meta_dir / "artist_canon.tsv")
        # The catalogue shortcut is for rows filed under their composer, the
        # way albumart recognises one: a performer's bucket can hold Mozart's
        # K. 545 and Scarlatti's K. 545 (Codex review of #255).
        from .classical_composer import CANON_FILENAME, load_composer_canon

        composers = set(load_composer_canon(cfg.meta_dir / CANON_FILENAME).values())

        # Load all catalogued rows
        rows = ctx.conn.execute(
            """
            SELECT file_path, artist, title, album, bitrate, size_bytes, duration, audio_hash
            FROM archive
            WHERE status = 'CATALOGUED'
              AND artist IS NOT NULL AND trim(artist) != ''
              AND title  IS NOT NULL AND trim(title)  != ''
            ORDER BY artist, title
            """
        ).fetchall()

        result.files_processed = len(rows)

        # Exact groups each path belongs to. A pair already in one EXACT
        # group is the same recording and needs no NEAR row as well. This
        # skipped any file that had EVER been in an EXACT group, whoever
        # the other file was: most originals arrived twice (NUC and USB1),
        # so none was compared with the baked copy it replaces, and 510 of
        # those copies were filed beside their originals (2026-09-26).
        exact_groups: dict[str, set[str]] = {}
        for row in ctx.conn.execute(
            "SELECT group_id, file_path FROM duplicates WHERE duplicate_type='EXACT'"
        ).fetchall():
            exact_groups.setdefault(row["file_path"], set()).add(row["group_id"])

        # Pre-load already-staged near dupe pairs to avoid re-flagging
        existing_near: set[tuple[str, str]] = set()
        near_rows: dict[str, list] = {}
        for row in ctx.conn.execute(
            "SELECT group_id, file_path, status, audio_hash FROM duplicates "
            "WHERE duplicate_type='NEAR'"
        ).fetchall():
            existing_near.add((row["group_id"], row["file_path"]))
            near_rows.setdefault(row["group_id"], []).append(row)

        # A pair closed as 'stale' -- a file changed under it, or it was
        # joined to a group that had -- was never flagged again, because any
        # row counted as "already flagged". 7 baked copies sat stuck beside
        # their originals (2026-09-26). Found again, it is judged again, with
        # the recordings there now.
        stale_near = {
            gid for gid, rs in near_rows.items() if all(r["status"] == "stale" for r in rs)
        }

        # Keep both: every member of a NEAR or ACOUSTIC group marked 'keep'
        # (the resolver keeps ONE; both kept is a person's decision, as Grey's
        # 29 AcoustID keep-boths of 2026-09-27). Held by the two recordings, not
        # the two paths, so a rename does not undo it.
        decided: dict[str, list] = {}
        for row in ctx.conn.execute(
            "SELECT group_id, status, audio_hash FROM duplicates "
            "WHERE duplicate_type IN ('NEAR', 'ACOUSTIC')"
        ).fetchall():
            decided.setdefault(row["group_id"], []).append(row)
        keep_both = {
            frozenset(r["audio_hash"] for r in rs)
            for rs in decided.values()
            if len(rs) > 1
            and all(r["status"] == "keep" and r["audio_hash"] for r in rs)
            and len({r["audio_hash"] for r in rs}) == 2
        }
        # A recording a person kept beside another never takes the catalogue
        # shortcut: a third copy paired with each of the two let the resolver
        # move one of them (Codex review of #255).
        kept_by_person: set[str] = set().union(*keep_both)

        # Bucket tracks by canonical artist
        artist_buckets: dict[str, list[dict]] = {}
        canonical_of_key: dict[str, str] = {}
        for row in rows:
            raw_artist = row["artist"].strip()
            canonical = artist_canon.resolve(raw_artist) or raw_artist
            key = _normalise(canonical)
            canonical_of_key.setdefault(key, canonical)
            bucket = artist_buckets.setdefault(key, [])
            bucket.append(dict(row))

        new_groups = 0
        new_pairs = 0

        # Collab credits to the plain name's bucket: [(plain key, credit tracks)].
        collab_of: dict[str, list[dict]] = {}
        for key, tracks in artist_buckets.items():
            raw = canonical_of_key.get(key, "")
            primary = _primary_artist_key(raw)
            if primary and primary != key and primary in artist_buckets:
                collab_of.setdefault(primary, []).extend(tracks)

        def _candidate_pairs():
            for key, tracks in artist_buckets.items():
                for i in range(len(tracks)):
                    for j in range(i + 1, len(tracks)):
                        yield key, tracks[i], tracks[j], False
                for a_ in tracks:
                    for b_ in collab_of.get(key, ()):
                        yield key, a_, b_, True

        for artist_key, a, b, cross in _candidate_pairs():
            if cross and abs((a["duration"] or 0) - (b["duration"] or 0)) > _COLLAB_SECONDS:
                continue

            # Skip if the two are already an exact pair
            if exact_groups.get(a["file_path"], set()) & exact_groups.get(b["file_path"], set()):
                continue

            # Don't merge two different live recordings of the same
            # song — "live" is stripped before scoring below, so
            # without this guard they'd look identical and collapse
            # into one group. Studio-vs-live still matches fine
            # (only one side carries the marker).
            # The resolver's own rule, title AND album: read from the title
            # alone, "Unplugged" and "24 Nights (Live)" were one song, and one
            # live recording moved to review (review of #86, finding 5).
            # One movement of one work, filed under its composer: one copy per
            # piece, live or not (Grey, 2026-10-24).
            da, db = a["duration"] or 0, b["duration"] or 0
            piece = (
                a["audio_hash"] not in kept_by_person
                and b["audio_hash"] not in kept_by_person
                and a["artist"].strip() in composers
                and b["artist"].strip() in composers
                # A copy under half the other's length is a cut, not the piece: a
                # 75 s "Air (Radio Edit)" beside the 320 s Air (Codex review of #255).
                and 2 * min(da, db) >= max(da, db)
                and _same_piece(a["title"], b["title"])
            )
            if not piece and _is_live(dict(a)) and _is_live(dict(b)) and not _same_take(a, b):
                continue

            # A medley is never one of the songs it holds, nor an improvisation
            # the piece it is played on (Codex review of #255).
            if bool(_NOT_THE_PIECE_RE.search(a["title"])) != bool(
                _NOT_THE_PIECE_RE.search(b["title"])
            ):
                continue

            if frozenset((a["audio_hash"], b["audio_hash"])) in keep_both:
                continue

            # Different movements of one work are different pieces.
            # The shared work title makes them score in the 90s, so
            # the threshold cannot catch this -- only the marker can.
            if _is_different_piece(a["title"], b["title"]):
                continue

            title_a = _normalise(a["title"], strip_qualifiers=True)
            title_b = _normalise(b["title"], strip_qualifiers=True)

            # Also compare with the artist's name taken off the front of either
            # title; the best of the four wins. Never lowers a score.
            bare_a = _without_artist_prefix(title_a, artist_key)
            bare_b = _without_artist_prefix(title_b, artist_key)
            score = max(
                fuzz.ratio(title_a, title_b),
                fuzz.ratio(bare_a, title_b),
                fuzz.ratio(title_a, bare_b),
                fuzz.ratio(bare_a, bare_b),
            )
            if score < TITLE_THRESHOLD and not piece:
                # The same song with and without its guest named: "Smooth (feat.
                # Rob Thomas)" and "Smooth" (Grey, 2026-10-12: the same song).
                # Only at one length -- "Stay With Me (With The Royal
                # Philharmonic Orchestra)" is another recording -- and never
                # when both name a guest and the guests differ.
                ca, cb = _credit(a["title"]), _credit(b["title"])
                if not (ca or cb) or (ca and cb and ca != cb):
                    continue
                # One take's length: the Complete Decca "Blues Are Brewin'"
                # holds alternate takes 2.5 s apart (pre-merge review of #191).
                if abs((a["duration"] or 0) - (b["duration"] or 0)) > _SAME_TAKE_SECONDS:
                    continue
                bare = fuzz.ratio(
                    _normalise(_CREDIT_BRACKET.sub(" ", a["title"]), strip_qualifiers=True),
                    _normalise(_CREDIT_BRACKET.sub(" ", b["title"]), strip_qualifiers=True),
                )
                if bare < TITLE_THRESHOLD:
                    continue
                score = bare
            elif (
                not piece
                and _is_live(dict(a)) != _is_live(dict(b))
                and (
                    abs((a["duration"] or 0) - (b["duration"] or 0)) > _SAME_TAKE_SECONDS
                    and fuzz.ratio(
                        _normalise(a["title"], strip_qualifiers=True, fold_and=False),
                        _normalise(b["title"], strip_qualifiers=True, fold_and=False),
                    )
                    < TITLE_THRESHOLD
                )
            ):
                # Matched only because "&" is "and" now, one side live, at
                # another length: a live take and a studio one (Stray Cats
                # "Rev It Up & Go (Live)", 83 s longer). Without the fold this
                # pair was never staged, so it is not staged now (pre-merge
                # review of #191). Studio-vs-live pairs that matched before
                # are left exactly as they were.
                continue

            # Near duplicate found
            gid = _group_id(a["file_path"], b["file_path"])
            confidence = round(score / 100.0, 4)

            reopened = gid in stale_near
            if reopened:
                stale_near.discard(gid)
                new_groups += 1
                result.files_changed += 1
                if not dry_run:
                    ctx.conn.execute(
                        """
                        UPDATE duplicates
                           SET status = 'pending', run_id = ?, confidence = ?,
                               audio_hash = (SELECT audio_hash FROM archive
                                              WHERE archive.file_path = duplicates.file_path)
                         WHERE group_id = ?
                        """,
                        (ctx.run_id, confidence, gid),
                    )
                    ctx.log_event(
                        "NEAR_DUPLICATE_FOUND",
                        file_path=a["file_path"],
                        stage=self.NAME,
                        note=f"group={gid} score={score} judged again (was stale)",
                    )
            # A member whose row has gone is added back below: reopened alone, the
            # lone copy was moved with no keeper (2026-10-25, Prince "Sign O' The
            # Times").

            is_new_group = False
            for fp in (a["file_path"], b["file_path"]):
                pair_key = (gid, fp)
                if pair_key not in existing_near:
                    is_new_group = True
                    new_pairs += 1
                    existing_near.add(pair_key)

                    if not dry_run:
                        ctx.conn.execute(
                            """
                            INSERT OR IGNORE INTO duplicates
                                (group_id, file_path, duplicate_type,
                                 confidence, run_id, audio_hash)
                            VALUES (?, ?, 'NEAR', ?, ?,
                                    (SELECT audio_hash FROM archive WHERE file_path = ?))
                            """,
                            (gid, fp, confidence, ctx.run_id, fp),
                        )
                        ctx.log_event(
                            "NEAR_DUPLICATE_FOUND",
                            file_path=fp,
                            stage=self.NAME,
                            note=(
                                f"group={gid} score={score} "
                                f"title_a={a['title']!r} "
                                f"title_b={b['title']!r}"
                            ),
                        )
            if is_new_group and not reopened:
                new_groups += 1
                result.files_changed += 1
                logger.info(
                    "near-dupe: %r ~~ %r (score=%d, artist=%s)",
                    a["title"],
                    b["title"],
                    score,
                    artist_key,
                )

        if not dry_run and new_pairs > 0:
            ctx.conn.commit()

        prefix = "Would stage" if dry_run else "Staged"
        if new_groups == 0:
            result.notes.append("No new near-duplicates found.")
        else:
            result.notes.append(
                f"{prefix} {new_groups} near-duplicate group(s) "
                f"({new_pairs} file entries). "
                f"Review with: musaeus dedupe"
            )

        ctx.record_stage(result)
        return result

    # ── Dry run ───────────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._detect(ctx, dry_run=True)

    # ── Run ───────────────────────────────────────────────────────────────────

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """A near-duplicate this stage found must actually be staged.

        Same contract as cross-dupe: the event is the report, the
        `duplicates` row is what the resolver acts on. One without the
        other is a finding that reaches a log and nothing else.
        """
        rows = ctx.conn.execute(
            "SELECT file_path FROM events WHERE run_id = ? "
            " AND event_type = 'NEAR_DUPLICATE_FOUND' ORDER BY id DESC LIMIT 10",
            (ctx.run_id,),
        ).fetchall()
        if not rows:
            return []
        unstaged = [
            Path(r["file_path"]).name
            for r in rows
            if not ctx.conn.execute(
                "SELECT 1 FROM duplicates WHERE file_path = ?  AND duplicate_type = 'NEAR' LIMIT 1",
                (r["file_path"],),
            ).fetchone()
        ]
        if not unstaged:
            return []
        return [
            f"{len(unstaged)} of {len(rows)} near-duplicate(s) were reported but "
            f"never staged: {', '.join(unstaged[:3])}"
        ]

    def run(self, ctx: RunContext) -> StageResult:
        return self._detect(ctx, dry_run=False)
