#!/usr/bin/env python3
"""
MUSAEUS — Stage: Normalize
Metadata normalisation for CATALOGUED archive rows.

What it does:
  - Fixes article suffixes in artist fields:
      "Beatles, The"         → "The Beatles"   (stored form → display form)
      "Cranberries, The (the)" → "The Cranberries"
  - Repairs ALL-CAPS fields:
      "FLEETWOOD MAC"        → "Fleetwood Mac"
      "DREAMS"               → "Dreams"
  - Applies title-case rules to title / album fields that are fully uppercase
  - Writes normalised values back to the archive table
  - Logs NORMALIZE_ARTIST / NORMALIZE_TITLE / NORMALIZE_ALBUM per change
  - dry_run() reports all proposed changes without touching the DB
  - Re-run safe: unchanged rows are skipped (no spurious events)

Rules:
  - Only modifies fields that ARE wrong — never touches already-correct data
  - Does NOT rename files on disk (that's Tagger's job after human approval)
  - Does NOT enforce ArtistCanon lookup (that's Scholar/Enrich)
  - Periodic DB commits every _COMMIT_EVERY files

ORPHEUS equivalents:
  - SCRIPTS/cleanup_embedded_article_names.py  (article fix)
  - SCRIPTS/find_all_caps_metadata.py          (caps detection)
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

# Short words that stay lowercase in title-case (standard English list)
_LOWERCASE_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "but",
        "or",
        "nor",
        "for",
        "so",
        "yet",
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
        "feat",
        "ft",
    }
)


# ── Normalisation helpers ──────────────────────────────────────────────────────


def _fix_article_suffix(name: str) -> str:
    """
    Convert stored article-suffix form to natural display form.
    "Beatles, The (the)" → "The Beatles"
    "Refused"            → "Refused"  (unchanged)
    """
    s = name.strip()

    m = _ARTICLE_SUFFIX_RE.search(s)
    if m:
        article = m.group(1).strip().capitalize()
        base = s[: m.start()].strip().rstrip(",").strip()
        return f"{article} {base}"

    m2 = _ARTICLE_COMMA_RE.search(s)
    if m2:
        article = m2.group(1).strip().capitalize()
        base = s[: m2.start()].strip()
        return f"{article} {base}"

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
    Convert a string to title-case, keeping short prepositions/articles
    lowercase except at the start of the string.
    """
    words = s.split()
    result = []
    for i, word in enumerate(words):
        clean = word.strip("\"'()[]{}.,!?")
        lower = clean.lower()
        if i == 0 or lower not in _LOWERCASE_WORDS:
            result.append(word.capitalize())
        else:
            result.append(word.lower())
    return " ".join(result)


def _normalise_artist(artist: str) -> str | None:
    """
    Return corrected artist string, or None if no change needed.
    Order: article fix first, then caps fix.
    """
    fixed = _fix_article_suffix(artist)
    if _is_all_caps(fixed):
        fixed = _smart_title_case(fixed)
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
