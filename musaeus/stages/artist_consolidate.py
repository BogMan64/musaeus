#!/usr/bin/env python3
"""
MUSAEUS — Artist Consolidation Stage

Normalizes artist names to canonical forms in the database.

What it does:
  1. Detects artist name variants (punctuation/spacing differences)
  2. Groups variants under a canonical form
  3. Updates database artist field to use canonical name
  4. Logs changes for review

Examples of consolidation:
  - "Andrews Sisters" + "Andrews Sisters (The)" → "Andrews Sisters (The)"
  - "Earth Wind and Fire" + "Earth, Wind & Fire" → "Earth, Wind & Fire"
  - "AC/DC" + "Ac/dc" + "AC-DC" → "AC-DC"

Based on ORPHEUS fix_artist_folder_variants.py
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING

from ..context import StageResult
from .base import BaseStage

if TYPE_CHECKING:
    from ..context import RunContext

logger = logging.getLogger(__name__)

# Known canonical artist display names (from ORPHEUS)
CANON_ARTIST_DISPLAY = {
    "98": "98°",
    "abba": "ABBA",
    "ac dc": "AC-DC",
    "a ha": "a-ha",
    "crosby stills and nash": "Crosby, Stills & Nash",
    "crosby stills nash and young": "Crosby, Stills, Nash & Young",
    "adam and the ants": "Adam & The Ants",
    "barney bentall and the legendary hearts": "Barney Bentall & The Legendary Hearts",
    "earth wind and fire": "Earth, Wind & Fire",
    "hall and oates": "Hall & Oates",
    "huey lewis and the news": "Huey Lewis & The News",
    "joan jett and the blackhearts": "Joan Jett & The Blackhearts",
    "kc and the sunshine band": "KC & The Sunshine Band",
    "kool and the gang": "Kool & The Gang",
    "martha and the vandellas": "Martha & The Vandellas",
    "of monsters and men": "Of Monsters and Men",
    "simon and garfunkel": "Simon & Garfunkel",
    "tom petty and the heartbreakers": "Tom Petty & The Heartbreakers",
    "andrews sisters": "Andrews Sisters (The)",
}

# Protected artist names (don't modify these)
PROTECTED_FULL_ARTIST_NAMES = {
    "crosby, stills & nash",
    "crosby, stills, nash & young",
    "earth, wind & fire",
    "simon & garfunkel",
    "hall & oates",
    "adam & the ants",
    "barney bentall & the legendary hearts",
    "of monsters and men",
    "andrews sisters (the)",
}


def _collapse_spaces(value: str) -> str:
    """Collapse multiple spaces into single space."""
    return re.sub(r"\s+", " ", value).strip()


def _strip_collaborator_tail(text: str) -> str:
    """Remove 'feat.', 'with', etc. suffixes from artist names."""
    raw = (text or "").strip()
    if not raw:
        return raw

    if raw.lower() in PROTECTED_FULL_ARTIST_NAMES:
        return raw

    # Remove collaborator suffixes
    raw = re.split(
        r"(?i)\s+(?:feat\.?|featuring|ft\.?|with|duet with|vs\.?|versus)\s+",
        raw,
        maxsplit=1,
    )[0].strip()

    if raw.lower() in PROTECTED_FULL_ARTIST_NAMES:
        return raw

    # Remove comma-separated additional artists
    if "," in raw and "&" not in raw:
        raw = raw.split(",", 1)[0].strip()

    return raw


def _normalize_key(text: str) -> str:
    """
    Normalize artist name to a comparison key.
    Example: "AC/DC" → "ac dc", "Earth, Wind & Fire" → "earth wind and fire"
    """
    text = _strip_collaborator_tail(text or "")
    text = text.replace("°", "")
    text = text.replace("&", " and ")
    text = text.replace("'", "'").replace("`", "'")
    text = re.sub(r"(?i)\(\s*the\s*\)", " the ", text)
    text = text.lower()
    # Remove all punctuation except spaces
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return _collapse_spaces(text)


def _smart_title(text: str) -> str:
    """Convert text to Title Case with special handling for short words."""
    words = []
    for word in text.split():
        if word.lower() in {"and", "of", "the"}:
            words.append(word.lower())
        elif word.isupper() and len(word) <= 5:
            words.append(word)  # Keep acronyms like ABBA
        else:
            words.append(word[:1].upper() + word[1:].lower())
    return " ".join(words)


def _preferred_name(names: list[str]) -> str:
    """
    Choose the best canonical name from a list of variants.
    
    Rules (in order):
    1. Check if any variant has a known canonical form
    2. Handle "The" prefix intelligently:
       - If variants exist with/without "The", use format: "Name (The)"
       - Examples: "Eagles" + "The Eagles" → "Eagles (The)"
    3. Otherwise, pick the longest/best-looking variant
    4. Apply smart title casing
    """
    cleaned = [_strip_collaborator_tail(n) for n in names if n.strip()]
    cleaned = [c for c in cleaned if c]
    if not cleaned:
        return "Unknown Artist"

    # Check for known canonical forms first
    keys = [_normalize_key(name) for name in cleaned]
    for key in keys:
        if key in CANON_ARTIST_DISPLAY:
            return CANON_ARTIST_DISPLAY[key]

    # Smart handling of "The" prefix variants
    # If we have both "Band Name" and "The Band Name", prefer "Band Name (The)"
    with_the = [n for n in cleaned if n.lower().startswith("the ")]
    without_the = [n for n in cleaned if not n.lower().startswith("the ")]
    
    if with_the and without_the:
        # We have both variants - use "Name (The)" format
        # Pick the version without "The" as base
        base = sorted(without_the, key=lambda s: (len(s), s.lower()), reverse=True)[0]
        return f"{_smart_title(base)} (The)"

    # Pick longest variant (usually most complete)
    best = sorted(cleaned, key=lambda s: (len(s), s.lower()), reverse=True)[0]
    best_key = _normalize_key(best)

    # Return known canon or smart-titled version
    return CANON_ARTIST_DISPLAY.get(best_key, _smart_title(best))


class ArtistConsolidateStage(BaseStage):
    """Normalize artist names to canonical forms."""

    NAME = "artist-consolidate"

    def validate(self, ctx: RunContext) -> None:
        count = ctx.conn.execute(
            "SELECT COUNT(DISTINCT artist) FROM archive WHERE status='CATALOGUED'"
        ).fetchone()[0]
        logger.info("[artist-consolidate] %d distinct artists to check", count)

    def _consolidate(self, ctx: RunContext, dry_run: bool) -> StageResult:
        """Consolidate artist name variants."""
        result = self._make_result(dry_run=dry_run)
        conn = ctx.conn

        # Get all distinct artists
        rows = conn.execute(
            """
            SELECT DISTINCT artist, COUNT(*) as track_count
            FROM archive
            WHERE status = 'CATALOGUED' AND artist IS NOT NULL
            GROUP BY artist
            ORDER BY artist
            """
        ).fetchall()

        if not rows:
            logger.info("[artist-consolidate] No artists to consolidate")
            result.notes.append("No artists found")
            ctx.record_stage(result)
            return result

        logger.info(f"[{self.NAME}] Analyzing {len(rows):,} distinct artists...")

        # Group artists by normalized key
        grouped: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for row in rows:
            artist = row["artist"]
            track_count = row["track_count"]
            key = _normalize_key(artist)
            if key:
                grouped[key].append((artist, track_count))

        # Find groups with multiple variants
        changes: dict[str, str] = {}  # old_name → new_name
        for key, variants in grouped.items():
            if len(variants) == 1:
                continue  # No consolidation needed

            # Pick canonical name
            variant_names = [v[0] for v in variants]
            canonical = _preferred_name(variant_names)

            # Map all non-canonical variants to canonical
            for variant_name, track_count in variants:
                if variant_name != canonical:
                    changes[variant_name] = canonical
                    logger.info(
                        f"[{self.NAME}] '{variant_name}' ({track_count} tracks) → '{canonical}'"
                    )
                    result.files_changed += track_count

        if not changes:
            result.notes.append("✓ No artist name variants found")
            ctx.record_stage(result)
            return result

        # Apply changes
        if not dry_run:
            for old_name, new_name in changes.items():
                conn.execute(
                    "UPDATE archive SET artist = ? WHERE artist = ? AND status = 'CATALOGUED'",
                    (new_name, old_name),
                )
                ctx.log_event(
                    "ARTIST_CONSOLIDATED",
                    stage=self.NAME,
                    note=f"'{old_name}' → '{new_name}'",
                )
            conn.commit()

        # Summary
        prefix = "Would consolidate" if dry_run else "Consolidated"
        result.notes.append(
            f"{prefix} {len(changes)} artist variant(s) affecting {result.files_changed} track(s)"
        )

        # Show sample changes
        sample = list(changes.items())[:10]
        for old, new in sample:
            result.notes.append(f"  '{old}' → '{new}'")
        if len(changes) > 10:
            result.notes.append(f"  ... and {len(changes) - 10} more")

        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._consolidate(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._consolidate(ctx, dry_run=False)
