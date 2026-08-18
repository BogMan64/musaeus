r"""
MUSAEUS — Sanitize Stage

Filesystem-safe metadata normalization for Windows/ExFAT/Android compatibility.

Fixes:
  - Forbidden characters: < > : " / \ | ? *
  - Trailing dots and spaces (Windows issue)
  - Smart quotes → straight quotes
  - Preserves readability while ensuring cross-platform compatibility

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

# Windows/ExFAT forbidden characters in filenames
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
      - / → - (forward slash to hyphen)
      - \\ → - (backslash to hyphen)
      - : → " - " (colon to space-hyphen-space for readability)
      - Remove: ? * " < > |
      - Smart quotes → straight quotes
      - Strip trailing dots and spaces
      - Collapse multiple spaces
    """
    if not value or not isinstance(value, str):
        return value

    v = value.strip()
    if not v:
        return None

    # Replace slashes with hyphens
    v = v.replace("/", "-").replace("\\", "-")

    # Replace colon with space-hyphen-space (more readable)
    v = v.replace(":", " - ")

    # Remove forbidden characters
    v = re.sub(r'[?*"<>|]', "", v)

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

    # Check for forbidden chars
    if FORBIDDEN_CHARS_RE.search(value):
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

            # Check each field
            for field in ("artist", "album", "title", "genre"):
                original = row[field]
                if not original:
                    continue

                if needs_sanitization(original):
                    sanitized = sanitize_value(original)
                    if sanitized and sanitized != original:
                        changes[field] = sanitized
                        fields_fixed[field] += 1
                        logger.info(
                            f"[{self.NAME}] {field}: '{original}' → '{sanitized}'"
                        )

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
