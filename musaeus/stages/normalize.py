#!/usr/bin/env python3
"""
MUSAEUS — Stage: Normalize
Metadata normalisation for CATALOGUED archive rows.

What it does:
  - Moves leading articles to suffix (ORPHEUS-style canonical storage):
      "The Beatles"         → "Beatles, The"
      "The Band"            → "Band, The"
      "A Tribe Called Quest" → "Tribe Called Quest, A"
  - This enables proper alphabetical sorting (B not T)
  - Repairs ALL-CAPS fields with MusicBrainz-style title case:
      "FLEETWOOD MAC"        → "Fleetwood Mac"
      "DREAMS"               → "Dreams"
      "ROCK 'N' ROLL"        → "Rock 'n' Roll"
      "DON'T STOP BELIEVIN'" → "Don't Stop Believin'"
  - Preserves acronyms and special terms:
      "AC/DC"                → "AC/DC" (not "Ac/dc")
      "R&B"                  → "R&B" (not "R&b")
      "USA"                  → "USA" (not "Usa")
  - Applies title-case rules to title/album fields that are fully uppercase
  - Writes normalised values back to the archive table
  - Logs NORMALIZE_ARTIST / NORMALIZE_TITLE / NORMALIZE_ALBUM per change
  - dry_run() reports all proposed changes without touching the DB
  - Re-run safe: unchanged rows are skipped (no spurious events)

Title Case Rules (MusicBrainz standard):
  - First word always capitalized
  - Last word always capitalized
  - Short prepositions/articles stay lowercase (unless first/last)
  - Acronyms preserved (AC/DC, USA, REM, etc.)
  - Special patterns: Rock 'n' Roll, R&B, feat.
  - Roman numerals stay uppercase (II, III, IV, etc.)

Article Storage Philosophy (ORPHEUS-compatible):
  - Database stores: "Beatles, The" (sorts under B)
  - Tagger can optionally write: "The Beatles" (display form)
  - This matches ORPHEUS canonical storage format

Rules:
  - Only modifies fields that ARE wrong — never touches already-correct data
  - Does NOT rename files on disk (that's Tagger's job after human approval)
  - Does NOT enforce ArtistCanon lookup (that's Scholar/Enrich)
  - Periodic DB commits every _COMMIT_EVERY files

ORPHEUS equivalents:
  - SCRIPTS/cleanup_embedded_article_names.py  (article fix)
  - SCRIPTS/find_all_caps_metadata.py          (caps detection)
  - SCRIPTS/audit_mb_title_case.py             (MusicBrainz title case)
  - SCRIPTS/orpheus_metadata_normalizer.py     (combined pass)
"""

from __future__ import annotations

import logging
import re
import unicodedata

from ..context import RunContext, StageResult
from .base import BaseStage

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 100

# ── Article-suffix regex (mirrors enrich.py _ARTICLE_SUFFIX_RE) ───────────────

_ARTICLE_SUFFIX_RE = re.compile(
    r"""
    ,?\s*
    \(\s*
    (the|a|an|le|la|les|el|los|las|de|het|een|die|das|ein|eine)
    \s*\)
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)
_ARTICLE_COMMA_RE = re.compile(
    r",\s*(the|a|an|le|la|les|el|los|las|de|het|een|die|das|ein|eine)\s*$",
    re.IGNORECASE,
)

# Strict Roman numeral (1-4999). _KEEP_CAPS previously hardcoded only I-X,
# so anything past X was title-cased into nonsense: "PART XIV" -> "Part Xiv",
# "CHAPTER XII" -> "Chapter Xii". A regex covers the real range instead --
# it matters for real music metadata (Chicago numbered albums run to XXXVIII;
# classical movements and acts routinely pass X).
_ROMAN_NUMERAL_RE = re.compile(r"^M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})$")

# Strings that are structurally valid Roman numerals but are really words or
# common music terms -- these must stay title-cased, not forced uppercase.
# "MIX" is the dangerous one (M+IX = 1009) and is everywhere in this library
# ("Radio MIX", "Extended MIX"); "CD"/"XL"/"MD"/"DIV" are the same trap.
_ROMAN_FALSE_FRIENDS: frozenset[str] = frozenset({"MIX", "CD", "MD", "XL", "DIV", "DIM"})

# Dotted abbreviation: "U.S.A.", "R.E.M.", "D.O.A." -- two or more
# letter-then-period pairs. _smart_title_case strips surrounding punctuation
# before its _KEEP_CAPS lookup, so "U.S.A." became "U.S.A" (not in the set),
# fell through to .capitalize(), and came out "U.s.a.".
_DOTTED_ABBREV_RE = re.compile(r"^(?:[A-Za-z]\.){2,}$")

# Real stylized band names whose leading word matches an entry in
# _move_article_to_suffix()'s article list but is actually part of the
# name, not a leading article -- must never be split. Confirmed live
# corruption (2026-08-16): "De La Soul" was mis-normalized to "La Soul,
# De" by a real Normalize run (2 real files affected, before this guard
# existed). The others below are documented, well-known real-world cases
# of the same failure shape (La Roux, Los Lobos, Los Lonely Boys, Die
# Ärzte, Das EFX) -- not present in this library today, but added
# defensively since the failure mode is identical and would otherwise
# silently corrupt them the moment they're ingested.
PROTECTED_ARTIST_NAMES: frozenset[str] = frozenset(
    {
        "de la soul",
        "la roux",
        "los lobos",
        "los lonely boys",
        "die ärzte",
        "die toten hosen",
        "das efx",
    }
)

# Short words that stay lowercase in title-case (MusicBrainz standard)
_LOWERCASE_WORDS: frozenset[str] = frozenset(
    {
        # Articles
        "a",
        "an",
        "the",
        # Conjunctions
        "and",
        "but",
        "or",
        "nor",
        "for",
        "so",
        "yet",
        # Prepositions
        "at",
        "by",
        "in",
        "of",
        "on",
        "to",
        "up",
        "as",
        "if",
        "vs",
        # Common in music titles
        "feat",
        "ft",
        "with",
        "from",
        "into",
        "onto",
        "upon",
        "n",
        "n'",  # Rock 'n' Roll
    }
)

# Words that should stay ALL CAPS (acronyms, special terms)
_KEEP_CAPS: frozenset[str] = frozenset(
    {
        "AC",
        "DC",
        "AC/DC",
        # Hyphenated forms added 2026-08-21. The slash forms alone were not
        # enough, because Sanitize rewrites "/" to "-" for filesystem safety
        # BEFORE Normalize runs -- so by the time the name reached this
        # lookup it was "AC-DC", which was not in the set, and it got
        # title-cased to "Ac-dc" on 92 files. The two stages were each
        # behaving correctly in isolation; the bug lived in the handoff.
        # Grey's call on the separator: hyphen, whatever is easiest on Linux.
        "AC-DC",
        "JAY-Z",
        "RUN-DMC",
        "D-A-D",
        "XL",  # XL Recordings -- also a Roman-numeral false friend (see below)
        "ACDC",
        "USA",
        "UK",
        "NYC",
        "LA",
        "DJ",
        "MC",
        "DMX",
        "REM",
        "INXS",
        "ELO",
        "OMD",
        "UB40",
        "TLC",
        "SWV",
        "II",
        "III",
        "IV",
        "V",
        "VI",
        "VII",
        "VIII",
        "IX",
        "X",  # Roman numerals
        "BMW",
        "UFO",
        "TV",
        "FM",
        "AM",
        "LP",
        "EP",
        "CD",
    }
)

# Special patterns to preserve
_SPECIAL_PATTERNS = [
    # 'n' combinations
    (re.compile(r"\b([Rr])ock\s*[''`]?\s*[Nn]\s*[''`]?\s*[Rr]oll\b"), r"\1ock 'n' Roll"),
    (re.compile(r"\bR\s*[''`&]\s*B\b", re.IGNORECASE), "R&B"),
    # Common abbreviations
    (re.compile(r"\bFt\.?\b", re.IGNORECASE), "feat."),
    (re.compile(r"\bFeat\.?\b", re.IGNORECASE), "feat."),
]


# ── Normalisation helpers ──────────────────────────────────────────────────────


def _move_article_to_suffix(name: str) -> str:
    """
    Move leading article to suffix for canonical storage (ORPHEUS-style).

    Examples:
      "The Beatles"     → "Beatles, The"
      "the Beatles"     → "Beatles, The" (case-insensitive match; the
                           suffix article is always emitted in its
                           canonical capitalized form regardless of input
                           casing, per the uniform-capitalization
                           convention -- matches artist_consolidate.py's
                           _smart_title() fix for the same ", The" vs
                           "(the)" convention)
      "A Tribe Called Quest" → "Tribe Called Quest, A"
      "An American Band" → "American Band, An"
      "Beatles, The"    → "Beatles, The" (already correct, unchanged)
      "Refused"         → "Refused" (no article, unchanged)

    This enables proper alphabetical sorting: "Beatles, The" sorts under B, not T.
    """
    s = name.strip()

    # Real stylized band name (De La Soul, Los Lobos, etc.) -- the
    # leading word looks like an article but isn't. Must be checked
    # before the suffix-format check below, since a protected name could
    # coincidentally also match _ARTICLE_SUFFIX_RE/_ARTICLE_COMMA_RE.
    if s.lower() in PROTECTED_ARTIST_NAMES:
        return s

    # Canonical ", The" suffix -- already correct, leave alone.
    if _ARTICLE_COMMA_RE.search(s):
        return s

    # Parenthetical "(the)" suffix. This is a real but WRONG form -- scope
    # doc §5 confirms ", The" is the convention and "(the)" was the source
    # of a three-times-regressed bug. Previously both regexes were OR'd
    # into a single "already has suffix format, return as-is" check, so a
    # "(the)" artist was classified as already-correct and passed through
    # untouched forever: Normalize could never fix it, because Normalize
    # believed there was nothing to fix. Confirmed live 2026-08-21 --
    # 1,262 archive rows across 203 distinct artists ("Archies (the)",
    # "5th Dimension (the)", "Animals (the)", ...) sitting in the wrong
    # form, versus 4,498 rows correctly in ", The". Convert instead of
    # accepting.
    m = _ARTICLE_SUFFIX_RE.search(s)
    if m:
        base = _ARTICLE_SUFFIX_RE.sub("", s).strip()
        # The base may already carry the canonical suffix -- the documented
        # double form "Beatles, The (the)". Strip the redundant parenthetical
        # and stop, rather than stacking a second article onto it.
        if _ARTICLE_COMMA_RE.search(base):
            return base
        base = base.rstrip(",").strip()
        if not base:
            return s
        return f"{base}, {m.group(1).capitalize()}"

    # Check for leading article, case-insensitively. Confirmed live-data
    # bug (2026-08-16): the original `s.startswith(f"{article} ")` check
    # is case-sensitive, so it silently skipped every lowercase-leading-
    # article artist -- "the Chieftains", "the Band", "the Bangles", and
    # 15 others found completely unfixed after a live Normalize run
    # (`Fixed: 0 artist(s)` despite 18 distinct affected artists in the
    # data).
    articles = [
        "The",
        "A",
        "An",
        "Le",
        "La",
        "Les",
        "El",
        "Los",
        "Las",
        "De",
        "Het",
        "Een",
        "Die",
        "Das",
        "Ein",
        "Eine",
    ]

    for article in articles:
        prefix = f"{article} "
        if s[: len(prefix)].lower() == prefix.lower() and len(s) > len(article) + 1:
            # Found leading article - move to suffix, canonical casing
            rest = s[len(article) :].strip()
            return f"{rest}, {article}"

    return s


def _is_all_caps(s: str) -> bool:
    """
    Return True if the string contains letters and they are ALL uppercase.
    Ignores numbers, punctuation, and strings with no letters.
    """
    letters = [c for c in s if unicodedata.category(c).startswith("L")]
    return bool(letters) and all(c.isupper() for c in letters)


def _smart_title_case(s: str) -> str:
    """
    Convert a string to MusicBrainz-style title case.

    Rules:
    - First word always capitalized
    - Last word always capitalized
    - Short prepositions/articles stay lowercase (unless first/last)
    - Preserve ALL-CAPS acronyms (AC/DC, USA, etc.)
    - Special patterns (Rock 'n' Roll, R&B, feat.)
    - Roman numerals stay uppercase
    """
    # Apply special patterns first
    result = s
    for pattern, replacement in _SPECIAL_PATTERNS:
        result = pattern.sub(replacement, result)

    words = result.split()
    if not words:
        return s

    processed = []
    for i, word in enumerate(words):
        # Clean word (remove surrounding punctuation for checking)
        clean = word.strip("\"'()[]{}.,!?;:-")
        upper_clean = clean.upper()

        # Dotted abbreviation ("U.S.A.", "R.E.M.") -- checked on the raw word,
        # before the punctuation-stripped lookups below, since stripping the
        # trailing period is exactly what used to break these.
        if _DOTTED_ABBREV_RE.match(word):
            processed.append(word.upper())
            continue

        # Check if it's an acronym/special term that should stay caps
        if upper_clean in _KEEP_CAPS:
            processed.append(word.replace(clean, upper_clean))
            continue

        # Roman numeral of any magnitude, excluding the real-word collisions
        # in _ROMAN_FALSE_FRIENDS. Only applied when the source token is
        # already all-caps -- a lowercase "mix"/"did" must never be promoted
        # to uppercase just because it happens to parse as a numeral.
        if (
            clean
            and clean.isupper()
            and upper_clean not in _ROMAN_FALSE_FRIENDS
            and _ROMAN_NUMERAL_RE.match(upper_clean)
        ):
            processed.append(word.replace(clean, upper_clean))
            continue

        # First or last word always capitalized
        if i == 0 or i == len(words) - 1:
            processed.append(word.capitalize())
            continue

        # Check if it's a lowercase word
        if clean.lower() in _LOWERCASE_WORDS:
            processed.append(word.lower())
            continue

        # Default: capitalize
        processed.append(word.capitalize())

    return " ".join(processed)


def _normalise_artist(artist: str) -> str | None:
    """
    Return corrected artist string, or None if no change needed.
    Order: caps fix first, then article move to suffix.
    """
    fixed = artist

    # Fix ALL-CAPS first
    if _is_all_caps(fixed):
        fixed = _smart_title_case(fixed)

    # Move article to suffix (ORPHEUS-style canonical form)
    fixed = _move_article_to_suffix(fixed)

    return fixed if fixed != artist else None


def _normalise_text_field(value: str) -> str | None:
    """
    Normalise a title or album field.
    Only fixes ALL-CAPS strings; does not apply article logic.
    Returns None if no change needed.
    """
    if _is_all_caps(value):
        return _smart_title_case(value)
    return None


# ── Stage ─────────────────────────────────────────────────────────────────────


class NormalizeStage(BaseStage):
    """
    Normalize — article-suffix fix + ALL-CAPS repair for archive metadata.
    """

    @classmethod
    def plan_candidates(cls, conn, cfg) -> tuple[int, str]:
        """Rows this stage would act on. Read-only; see planner.py."""
        n = conn.execute("SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'").fetchone()[0]
        return int(n), "rows whose artist/title would be normalised"

    NAME = "normalize"

    # ── Validate ──────────────────────────────────────────────────────────────

    def validate(self, ctx: RunContext) -> None:
        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'"
        ).fetchone()[0]
        logger.info("[normalize] %d CATALOGUED row(s) to inspect", count)

    # ── Shared logic ──────────────────────────────────────────────────────────

    def _normalize(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)

        rows = ctx.conn.execute(
            """
            SELECT file_path, artist, title, album
            FROM archive
            WHERE status = 'CATALOGUED'
              AND (artist IS NOT NULL OR title IS NOT NULL OR album IS NOT NULL)
            ORDER BY file_path
            """
        ).fetchall()

        artist_fixed = 0
        title_fixed = 0
        album_fixed = 0

        for row in rows:
            result.files_processed += 1
            fp = row["file_path"]
            changed = False

            artist = row["artist"] or ""
            title = row["title"] or ""
            album = row["album"] or ""

            new_artist = _normalise_artist(artist) if artist else None
            new_title = _normalise_text_field(title) if title else None
            new_album = _normalise_text_field(album) if album else None

            if new_artist:
                logger.info("[normalize] artist  %r → %r  (%s)", artist, new_artist, fp)
                if not dry_run:
                    ctx.conn.execute(
                        "UPDATE archive SET artist=? WHERE file_path=?",
                        (new_artist, fp),
                    )
                    ctx.log_event(
                        "NORMALIZE_ARTIST",
                        file_path=fp,
                        old_value=artist,
                        new_value=new_artist,
                        stage=self.NAME,
                    )
                artist_fixed += 1
                changed = True

            if new_title:
                logger.info("[normalize] title   %r → %r  (%s)", title, new_title, fp)
                if not dry_run:
                    ctx.conn.execute(
                        "UPDATE archive SET title=? WHERE file_path=?",
                        (new_title, fp),
                    )
                    ctx.log_event(
                        "NORMALIZE_TITLE",
                        file_path=fp,
                        old_value=title,
                        new_value=new_title,
                        stage=self.NAME,
                    )
                title_fixed += 1
                changed = True

            if new_album:
                logger.info("[normalize] album   %r → %r  (%s)", album, new_album, fp)
                if not dry_run:
                    ctx.conn.execute(
                        "UPDATE archive SET album=? WHERE file_path=?",
                        (new_album, fp),
                    )
                    ctx.log_event(
                        "NORMALIZE_ALBUM",
                        file_path=fp,
                        old_value=album,
                        new_value=new_album,
                        stage=self.NAME,
                    )
                album_fixed += 1
                changed = True

            if changed:
                result.files_changed += 1

            if result.files_processed % _COMMIT_EVERY == 0 and not dry_run:
                ctx.conn.commit()
                logger.info("[normalize] checkpoint %d", result.files_processed)

        prefix = "Would fix" if dry_run else "Fixed"
        result.notes.append(
            f"{prefix}: {artist_fixed} artist(s), {title_fixed} title(s), {album_fixed} album(s)."
        )

        ctx.record_stage(result)
        return result

    # ── Dry run / Run ─────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._normalize(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._normalize(ctx, dry_run=False)
