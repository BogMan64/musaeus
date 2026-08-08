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

# Short words that stay lowercase in title-case (MusicBrainz standard)
_LOWERCASE_WORDS: frozenset[str] = frozenset(
    {
        # Articles
        "a", "an", "the",
        # Conjunctions
        "and", "but", "or", "nor", "for", "so", "yet",
        # Prepositions
        "at", "by", "in", "of", "on", "to", "up", "as", "if", "vs",
        # Common in music titles
        "feat", "ft", "with", "from", "into", "onto", "upon",
        "n", "n'",  # Rock 'n' Roll
    }
)

# Words that should stay ALL CAPS (acronyms, special terms)
_KEEP_CAPS: frozenset[str] = frozenset(
    {
        "AC", "DC", "AC/DC", "ACDC", "USA", "UK", "NYC", "LA", "DJ", "MC",
        "DMX", "REM", "INXS", "ELO", "OMD", "UB40", "TLC", "SWV",
        "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",  # Roman numerals
        "BMW", "UFO", "TV", "FM", "AM", "LP", "EP", "CD",
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
      "The Band"        → "Band, The"
      "A Tribe Called Quest" → "Tribe Called Quest, A"
      "An American Band" → "American Band, An"
      "Beatles, The"    → "Beatles, The" (already correct, unchanged)
      "Refused"         → "Refused" (no article, unchanged)
    
    This enables proper alphabetical sorting: "Beatles, The" sorts under B, not T.
    """
    s = name.strip()
    
    # If already has suffix format, return as-is
    if _ARTICLE_SUFFIX_RE.search(s) or _ARTICLE_COMMA_RE.search(s):
        return s
    
    # Check for leading article
    articles = ["The", "A", "An", "Le", "La", "Les", "El", "Los", "Las", "De", "Het", "Een", "Die", "Das", "Ein", "Eine"]
    
    for article in articles:
        if s.startswith(f"{article} ") and len(s) > len(article) + 1:
            # Found leading article - move to suffix
            rest = s[len(article):].strip()
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
        
        # Check if it's an acronym/special term that should stay caps
        if upper_clean in _KEEP_CAPS:
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
