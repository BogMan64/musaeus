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


# Guest-clause markers: keyword + everything after it, to end of string.
# Matched via a trailing lookahead of (whitespace|end-of-string) instead of
# a required trailing \s+ delimiter, so a guest-clause phrase sitting at
# the very end of the string (e.g. "Artist and Friends", with nothing
# after "Friends") still gets stripped -- not just mid-string cases.
#
# Deliberately "feat\." (period required) or "featuring", never bare
# "feat" -- a bare word-boundary "feat" false-matched the real band name
# "Little Feat" (confirmed incident). Requiring the period disambiguates
# "feat." the collaborator abbreviation from "Feat" as part of a proper
# name.
_GUEST_CLAUSE_RE = re.compile(
    r"(?i)\s+(?:feat\.|featuring|ft\.?|with|duet with|vs\.?|versus"
    r"|special guest|and friends)(?=\s|$).*$"
)

# "the"-article, in any of the three real on-disk spellings this project
# actually uses: leading "The X", trailing "X, The" (comma-suffix -- the
# confirmed, dominant real convention), and parenthetical "X (The)" (an
# ORPHEUS-specific convention, not MUSAEUS's own). All three refer to the
# same fact (this artist name includes an article) and must be recognized
# uniformly, both for grouping (_normalize_key) and for canonical-format
# decisions (_preferred_name).
_LEADING_THE_RE = re.compile(r"(?i)^\s*the\s+")
_TRAILING_COMMA_THE_RE = re.compile(r"(?i),\s*the\s*$")
_PAREN_THE_RE = re.compile(r"(?i)\(\s*the\s*\)\s*$")


def _has_the_article(name: str) -> bool:
    """True if name carries a "the"-article in any of the three real
    spellings (leading/trailing-comma/parenthetical)."""
    return bool(
        _LEADING_THE_RE.match(name)
        or _TRAILING_COMMA_THE_RE.search(name)
        or _PAREN_THE_RE.search(name)
    )


def _strip_collaborator_tail(text: str) -> str:
    """
    Remove a guest-clause tail (feat./featuring/with/special guest/and
    Friends/etc. + everything after it), then normalize any remaining
    literal "and" join to "&" (e.g. "Simon and Garfunkel" -> "Simon &
    Garfunkel"), matching CANON_ARTIST_DISPLAY's existing convention.
    Order matters: guest-clause stripping must happen before the and->&
    conversion, so a stripped-off guest name's own "and" (if any) never
    gets a chance to be converted.
    """
    raw = (text or "").strip()
    if not raw:
        return raw

    if raw.lower() in PROTECTED_FULL_ARTIST_NAMES:
        return raw

    raw = _GUEST_CLAUSE_RE.sub("", raw).strip()

    if raw.lower() in PROTECTED_FULL_ARTIST_NAMES:
        return raw

    # Comma-tail: only a real collaborator credit if the text after the
    # first comma isn't an article (the/a/an, case-insensitive) --
    # "Beatles, The" is the confirmed real on-disk canonical suffix form
    # (341 real folders use it), not a collaborator credit, and must
    # survive this step untouched. Only "Artist, SomeoneElse"-shaped
    # comma tails get stripped.
    if "," in raw and "&" not in raw:
        head, _, tail = raw.partition(",")
        if tail.strip().lower() not in ("the", "a", "an"):
            raw = head.strip()

    if raw.lower() in PROTECTED_FULL_ARTIST_NAMES:
        return raw

    raw = re.sub(r"(?i)\band\b", "&", raw)

    return raw


def _normalize_key(text: str) -> str:
    """
    Normalize artist name to a comparison key.
    Example: "AC/DC" → "ac dc", "Earth, Wind & Fire" → "earth wind and fire"

    The "the"-article (in any of its three real spellings -- leading
    "The X", trailing "X, The", or parenthetical "X (The)") is stripped
    entirely from the key, not folded into a fixed position -- this makes
    "Chieftains", "the Chieftains", and (hypothetically) "Chieftains,
    The" all produce the identical key, so they group together for
    consolidation regardless of which article style was used. Which
    display format the eventual canonical name uses is decided
    separately by _preferred_name()'s own _has_the_article() check on
    the un-stripped cleaned names, not by this key.
    """
    text = _strip_collaborator_tail(text or "")
    text = text.replace("°", "")
    text = text.replace("&", " and ")
    text = text.replace("'", "'").replace("`", "'")
    text = _LEADING_THE_RE.sub("", text)
    text = _TRAILING_COMMA_THE_RE.sub("", text)
    text = _PAREN_THE_RE.sub("", text)
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


def _preferred_name(names_with_counts: list[tuple[str, int]]) -> str:
    """
    Choose the best canonical name from a list of (name, track_count)
    variants sharing the same normalized key.

    Rules (in order):
    1. Check if any variant has a known canonical form (CANON_ARTIST_DISPLAY)
    2. Handle "the"-article variants intelligently (any of the three real
       spellings, unified via _has_the_article -- not just leading "The"):
       - If variants exist with/without the article, use "Name (The)"
       - The non-article base is the highest-track_count variant.
    3. Otherwise, the highest-track_count variant wins as canonical (ties
       broken by longest string, then alphabetically for determinism).
       Previously: longest string only, with no track-count awareness --
       a real gap when two spelling/casing variants of the same artist
       had different track counts and the less-common spelling happened
       to be longer.
    4. Apply smart title casing.
    """
    cleaned = [
        (_strip_collaborator_tail(n), c) for n, c in names_with_counts if n and n.strip()
    ]
    cleaned = [(n, c) for n, c in cleaned if n]
    if not cleaned:
        return "Unknown Artist"

    # Check for known canonical forms first
    keys = [_normalize_key(n) for n, _ in cleaned]
    for key in keys:
        if key in CANON_ARTIST_DISPLAY:
            return CANON_ARTIST_DISPLAY[key]

    def _rank(item: tuple[str, int]) -> tuple[int, int, str]:
        name, count = item
        return (-count, -len(name), name.lower())

    # Smart handling of "the"-article variants (leading/trailing-comma/
    # parenthetical, unified) -- if we have both an article and a
    # non-article form, prefer "Name (The)", base picked by track count.
    with_the = [(n, c) for n, c in cleaned if _has_the_article(n)]
    without_the = [(n, c) for n, c in cleaned if not _has_the_article(n)]

    if with_the and without_the:
        base_name, _ = sorted(without_the, key=_rank)[0]
        return f"{_smart_title(base_name)} (The)"

    # Highest track_count wins; ties broken by longest string, then
    # alphabetically for determinism.
    best_name, _ = sorted(cleaned, key=_rank)[0]
    best_key = _normalize_key(best_name)

    # Return known canon or smart-titled version
    return CANON_ARTIST_DISPLAY.get(best_key, _smart_title(best_name))


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

            # Pick canonical name (variants is already list[(name, track_count)])
            canonical = _preferred_name(variants)

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
