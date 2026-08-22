r"""
MUSAEUS — Sanitize Stage

Filesystem-safe metadata normalization for Windows/ExFAT/Android compatibility.

Fixes, in the METADATA only:
  - Control characters
  - Trailing dots and spaces (Windows issue)
  - Smart quotes → straight quotes
  - Collapsed whitespace

Does NOT touch path-forbidden characters (< > : " / \ | ? *). Those are
unsafe in a PATH, not in a tag, and organize.sanitize_path_component()
already handles them wherever a path is built. Applying them here instead
corrupted the stored names -- "AC/DC" became "Ac-dc", "R&B/Funk/Soul"
became "R&B-Funk-Soul" -- while protecting nothing that was not already
protected. Narrowed 2026-08-22.

Based on ORPHEUS sanitize_component() function.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from ..context import StageResult
from .base import BaseStage

if TYPE_CHECKING:
    from ..context import RunContext

logger = logging.getLogger(__name__)

# Characters that are unsafe in METADATA itself: control characters only.
#
# Narrowed 2026-08-22 (Grey's call: match MusicBrainz exactly, sanitize only
# for the path). This stage used to apply Windows/ExFAT PATH rules to
# archive.artist/album/title -- but those are tags, and every path-building
# site already sanitizes independently (organize.build_track_filename,
# organize.sanitize_path_component, tribute_quarantine). So the path was
# never at risk; the only effect was corrupting the stored name.
#
# It cost us twice: "AC/DC" became the stored artist "Ac-dc" on 92 files
# (Normalize then title-cased what it no longer recognised), and
# "R&B/Funk/Soul" became "R&B-Funk-Soul" on 916, inventing a genre that
# matches no canon and generating ~1,000 phantom MasterLaw conflicts.
#
# MusicBrainz has no single house rule -- it records each artist's own
# styling ("Simon & Garfunkel", "Peter, Paul and Mary", "AC/DC"). Inheriting
# that is only possible if this stage stops overwriting it.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

# Kept for callers that genuinely build a path from a raw string.
FORBIDDEN_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Smart quotes and special dashes
SMART_QUOTE_MAP = {
    "\u2018": "'",  # '
    "\u2019": "'",  # '
    "\u201c": '"',  # "
    "\u201d": '"',  # "
    "\u2013": "-",  # –
    "\u2014": "-",  # —
    "\u2212": "-",  # −
}


def sanitize_value(value: str | None) -> str | None:
    """
    Make metadata filesystem-safe while preserving readability.

    Rules (from ORPHEUS):
      - Strip control characters
      - Smart quotes → straight quotes
      - Strip trailing dots and spaces
      - Collapse multiple spaces

    Deliberately does NOT touch / \\ : ? * " < > | -- those are unsafe in a
    PATH, not in a tag, and every path is built through
    organize.sanitize_path_component(). See the note on _CONTROL_CHARS_RE.
    """
    if not value or not isinstance(value, str):
        return value

    v = _CONTROL_CHARS_RE.sub("", value).strip()
    if not v:
        return None

    # Path-forbidden characters (/ \\ : ? * " < > |) are deliberately left
    # alone here -- see _CONTROL_CHARS_RE. organize.sanitize_path_component()
    # handles them at the moment a path is actually built, which is the only
    # moment they matter.

    # Convert smart quotes/dashes to ASCII equivalents
    for smart, plain in SMART_QUOTE_MAP.items():
        v = v.replace(smart, plain)

    # Collapse multiple spaces
    v = re.sub(r"\s+", " ", v).strip()

    # Remove trailing dots and spaces (Windows can't handle these)
    v = v.rstrip(". ")

    return v if v else None


def needs_sanitization(value: str | None) -> bool:
    """Check if a value contains filesystem-unsafe characters."""
    if not value:
        return False

    # Control characters are unsafe in metadata itself. Path-forbidden
    # characters are not this stage's business -- see _CONTROL_CHARS_RE.
    if _CONTROL_CHARS_RE.search(value):
        return True

    # Check for smart quotes/dashes
    if any(char in value for char in SMART_QUOTE_MAP):
        return True

    # Check for trailing dots/spaces
    return value != value.rstrip(". ")


class SanitizeStage(BaseStage):
    """Normalize metadata for cross-platform filesystem compatibility."""

    NAME = "sanitize"

    def validate(self, ctx: RunContext) -> None:
        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'"
        ).fetchone()[0]
        logger.info("[sanitize] %d CATALOGUED row(s) to inspect", count)

    def _sanitize(self, ctx: RunContext, dry_run: bool) -> StageResult:
        """Sanitize artist, album, title metadata for all CATALOGUED tracks."""
        result = self._make_result(dry_run=dry_run)
        conn = ctx.conn

        # Find tracks that need sanitization
        rows = conn.execute(
            """
            SELECT file_path, artist, album, title, genre
            FROM archive
            WHERE status = 'CATALOGUED'
              AND (artist IS NOT NULL OR album IS NOT NULL OR title IS NOT NULL)
            ORDER BY artist, album
            """
        ).fetchall()

        if not rows:
            logger.info(f"[{self.NAME}] No tracks to sanitize")
            return result

        logger.info(f"[{self.NAME}] Checking {len(rows):,} tracks for unsafe metadata...")

        sanitized_count = 0
        fields_fixed = {"artist": 0, "album": 0, "title": 0, "genre": 0}

        for row in rows:
            result.files_processed += 1
            file_path = row["file_path"]
            changes = {}

            # genre is deliberately NOT sanitised (removed 2026-08-21).
            #
            # These rules exist to make a string safe as a Windows/ExFAT PATH
            # COMPONENT, and artist/album/title genuinely become folder and
            # file names. Genre never does -- it is only ever a tag, and
            # playlist.py sanitises its own .m3u8 filenames separately via
            # _safe_genre(). Running path rules over it stripped the "/" out
            # of Apple's canonical genre names, turning "R&B/Funk/Soul" into
            # "R&B-Funk-Soul" on 904 files and "Disco/Electronic" into
            # "Disco-Electronic" -- silently inventing genre names that
            # match no canon, and generating ~1,000 phantom conflicts
            # against MasterLaw. Grey's preference is the "/" form.
            for field in ("artist", "album", "title"):
                original = row[field]
                if not original:
                    continue

                if needs_sanitization(original):
                    sanitized = sanitize_value(original)
                    if sanitized and sanitized != original:
                        changes[field] = sanitized
                        fields_fixed[field] += 1
                        logger.info(f"[{self.NAME}] {field}: '{original}' → '{sanitized}'")

            # Apply changes to DB
            if changes:
                result.files_changed += 1
                if not dry_run:
                    for field, new_value in changes.items():
                        conn.execute(
                            f"UPDATE archive SET {field} = ? WHERE file_path = ?",
                            (new_value, file_path),
                        )
                        ctx.log_event(
                            f"SANITIZE_{field.upper()}",
                            file_path=file_path,
                            old_value=row[field],
                            new_value=new_value,
                            stage=self.NAME,
                        )

                sanitized_count += 1

        if not dry_run:
            conn.commit()

        prefix = "Would sanitize" if dry_run else "Sanitized"
        if sanitized_count > 0:
            result.notes.append(
                f"{prefix} {sanitized_count:,} tracks "
                f"(artist: {fields_fixed['artist']}, "
                f"album: {fields_fixed['album']}, "
                f"title: {fields_fixed['title']}, "
                f"genre: {fields_fixed['genre']})"
            )
        else:
            result.notes.append("All metadata is filesystem-safe")

        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._sanitize(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._sanitize(ctx, dry_run=False)
