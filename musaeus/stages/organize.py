#!/usr/bin/env python3
r"""
MUSAEUS — Organize Stage

File organization and renaming stage for CATALOGUED archive rows.

What it does:
  - Strips track number prefixes from filenames:
      "171. Artist - Title.m4a" → "Artist - Title.m4a"
      "01 - Title.mp3"          → "Title.mp3"
      "Disc 2 - 05 - Title.flac" → "Title.flac"
  - Renames files to standard format:
      "random_name.m4a"         → "Artist - Title.m4a"
  - Organizes into Artist/Album/ folder structure:
      INBOX/flat/file.m4a       → INBOX/Artist/Album/Artist - Title.m4a
  - Sanitizes for Windows/ExFAT compatibility:
      Removes forbidden chars: \ / : * ? " < > |
      Handles reserved names: CON, PRN, AUX, NUL
      Normalizes quotes/dashes
  - Updates database with new file paths
  - Logs ORGANIZE_RENAME / ORGANIZE_MOVE events

Track Number Patterns (ORPHEUS-compatible):
  - "Disc 2 - 05 - Title"
  - "01-05 Title"
  - "171. Title"
  - "01 Title"
  - "171.Title"

Filename Format (ORPHEUS-compatible):
  - Standard: "Artist - Title.ext"
  - Protected artists preserved: "AC/DC", "ABBA", "98°"
  - Article suffix form used: "Beatles, The"

Rules:
  - Only processes CATALOGUED files with complete metadata
  - dry_run() shows all proposed changes without touching files
  - Re-run safe: already-organized files are skipped
  - Handles artist canon lookup from ArtistCanon
  - Creates target directories as needed
  - Atomic moves with unique_path() collision handling

ORPHEUS equivalents:
  - SCRIPTS/cleanup_library_names.py  (renaming)
  - SCRIPTS/organize_only.py          (folder organization)
  - SCRIPTS/lib/orpheus_naming.py     (naming logic)
"""

from __future__ import annotations

import logging
import re
import sqlite3
import unicodedata
from pathlib import Path

from ..context import RunContext, StageResult
from .base import BaseStage
from .sanitize import SMART_QUOTE_MAP

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 50

# ── Track Number Stripping Patterns (ORPHEUS-compatible) ─────────────────────

_TRACK_NUMBER_PATTERNS = [
    # "Disc 2 - 05 - Title" or "CD 1 - 03 - Title"
    re.compile(r"^\s*(?:disc|cd)\s*\d+\s*[-_. ]+\s*\d{1,3}\s*[-_. ]+", re.IGNORECASE),
    # "01-05 Title" or "1-3 Title"
    re.compile(r"^\s*\d{1,2}\s*[-_.]\s*\d{1,3}\s+"),
    # "171. Title" or "05. Title"
    re.compile(r"^\s*\d{2,3}\s*[-_.]\s+"),
    # "01 Title" (space after number)
    re.compile(r"^\s*0\d\s+"),
    # "171.Title" or "05.Title" (no space)
    re.compile(r"^\s*\d{2,3}[._]\s*"),
]

# ── Windows/ExFAT Forbidden Characters ────────────────────────────────────────

_FORBIDDEN_CHARS = r'\/:*?"<>|'
_FORBIDDEN_RE = re.compile(f"[{re.escape(_FORBIDDEN_CHARS)}]")

_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }
)

# ── Protected Artist Tokens (preserve special formatting) ─────────────────────

_PROTECTED_TOKENS = frozenset(
    {
        "ABBA",
        "AC-DC",
        "AC/DC",
        "BTO",
        "CCR",
        "CSNY",
        "INXS",
        "R.E.M.",
        "REM",
        "U2",
        "TLC",
        "SWV",
        "UB40",
        "DMX",
    }
)

_PROTECTED_ARTIST_NAMES = frozenset(
    {
        "crosby, stills & nash",
        "crosby, stills, nash & young",
        "earth, wind & fire",
        "simon & garfunkel",
        "hall & oates",
        "sly & the family stone",
    }
)

# ── Helper Functions ───────────────────────────────────────────────────────────


def strip_track_number_prefix(text: str) -> str:
    """
    Remove track number prefix from filename/title using ORPHEUS patterns.

    Examples:
      "171. Afrika Bambaataa - Planet Rock.m4a" → "Afrika Bambaataa - Planet Rock.m4a"
      "01 - Title.mp3"                          → "Title.mp3"
      "Disc 2 - 05 - Song.flac"                 → "Song.flac"
    """
    original = text.strip()

    for pattern in _TRACK_NUMBER_PATTERNS:
        stripped = pattern.sub("", original, count=1).strip()
        if stripped and stripped != original:
            return stripped

    return original


def sanitize_path_component(text: str) -> str:
    r"""
    Sanitize a single path component for Windows/ExFAT compatibility.

    - Removes forbidden characters: \ / : * ? " < > |
    - Handles Windows reserved names (CON, PRN, etc.)
    - Normalizes quotes and dashes
    - Strips control characters
    - Strips leading/trailing dots and spaces
    """
    if not text:
        return "Unknown"

    # Normalize unicode
    s = unicodedata.normalize("NFC", str(text))

    # Normalize smart quotes and dashes to ASCII. Reuses sanitize.py's
    # SMART_QUOTE_MAP rather than a second inline copy, so the two modules
    # cannot disagree about what "normalised" means. The previous inline
    # chain here was silently broken in both directions: its apostrophe
    # replacements mapped ASCII "'" to ASCII "'" (a no-op, so curly
    # U+2018/U+2019 were never touched), and its double-quote line was
    # parsed by Python as replace(", '", '"') -- a stray triple-quote
    # opened a string literal -- which rewrote the substring ", '" as a
    # quote character that the forbidden-char pass below then turned into
    # "_", mangling e.g. "Hello, 'Cause" into "Hello_Cause".
    for _smart, _plain in SMART_QUOTE_MAP.items():
        s = s.replace(_smart, _plain)
    s = s.replace("`", "'")

    # Remove control characters
    s = "".join(c for c in s if unicodedata.category(c)[0] != "C")

    # Replace forbidden characters with safe alternatives
    s = _FORBIDDEN_RE.sub("_", s)

    # Collapse multiple spaces
    s = re.sub(r"\s+", " ", s).strip()

    # Strip leading/trailing dots and spaces (Windows doesn't like them)
    s = s.strip(". ")

    if not s:
        return "Unknown"

    # Check for Windows reserved names
    name_upper = s.split(".")[0].upper()
    if name_upper in _WINDOWS_RESERVED_NAMES:
        s = f"_{s}"

    return s


def build_track_filename(artist: str, title: str, ext: str) -> str:
    """
    Build standard filename: "Artist - Title.ext"

    ORPHEUS-compatible format with sanitization.
    """
    artist_safe = sanitize_path_component(artist or "Unknown Artist")
    title_safe = sanitize_path_component(strip_track_number_prefix(title or "Unknown Title"))

    # Ensure extension starts with dot and is lowercase
    if not ext.startswith("."):
        ext = f".{ext}"
    ext = ext.lower()

    return f"{artist_safe} - {title_safe}{ext}"


def unique_path(target: Path) -> Path:
    """
    Return a unique path by appending (N) if target already exists.

    Example:
      "Beatles, The/Abbey Road/Beatles, The - Come Together.m4a"
      → "Beatles, The/Abbey Road/Beatles, The - Come Together (2).m4a"
    """
    if not target.exists():
        return target

    stem = target.stem
    suffix = target.suffix
    parent = target.parent

    counter = 2
    while True:
        new_path = parent / f"{stem} ({counter}){suffix}"
        if not new_path.exists():
            return new_path
        counter += 1


def _display_path(path: Path, root: Path) -> str:
    """Path relative to *root* for logging, falling back to the full path.

    Path.relative_to() raises ValueError when the path is not under root, and
    this is only ever used to make a log line shorter -- a cosmetic helper must
    not be able to abort a stage. It previously could: the raise happened
    before the dry-run check, so even `organize --dry-run` died on the first
    row that had moved outside INBOX.
    """
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


# ── Stage ──────────────────────────────────────────────────────────────────────


class OrganizeStage(BaseStage):
    """
    Organize — rename and reorganize files into Artist/Album/ structure.
    """

    NAME = "organize"

    def _apply_rename(
        self,
        ctx: RunContext,
        current_path: Path,
        target_path: Path,
        row_id: int,
        event_type: str,
    ) -> bool:
        """
        Rename on disk, then update the DB by rowid. If the DB write fails
        (e.g. a stale row already holds the target path), the filesystem
        rename is reverted so disk and DB never drift out of sync. Returns
        False so the caller can skip this row and keep processing the rest.
        """
        current_path.rename(target_path)
        try:
            ctx.conn.execute(
                "UPDATE archive SET file_path = ? WHERE rowid = ?",
                (str(target_path), row_id),
            )
        except sqlite3.IntegrityError as exc:
            logger.error(
                "[organize] DB collision for %s -> %s (%s); reverting move",
                current_path,
                target_path,
                exc,
            )
            try:
                target_path.rename(current_path)
            except OSError as revert_exc:
                logger.error(
                    "[organize] COULD NOT REVERT %s -- disk/DB now out of "
                    "sync, needs manual fix: %s",
                    current_path,
                    revert_exc,
                )
            return False

        ctx.log_event(
            event_type,
            file_path=str(current_path),
            old_value=str(current_path),
            new_value=str(target_path),
            stage=self.NAME,
        )
        return True

    def validate(self, ctx: RunContext) -> None:
        """Check how many files need organization."""
        count = ctx.conn.execute(
            """
            SELECT COUNT(*) FROM archive
            WHERE status = 'CATALOGUED'
              AND artist IS NOT NULL
              AND title IS NOT NULL
            """
        ).fetchone()[0]
        logger.info("[organize] %d CATALOGUED file(s) with metadata", count)

    def _organize(self, ctx: RunContext, dry_run: bool) -> StageResult:
        """Organize files: strip track numbers, rename, move to Artist/Album/."""
        result = self._make_result(dry_run=dry_run)

        # finalized_at IS NULL keeps this stage off files Finalize has already
        # moved into ALAC-Library. Those rows keep status='CATALOGUED', so
        # without this predicate they were selected here and their target_dir
        # was rebuilt under ctx.inbox -- i.e. Organize would have moved
        # finished library files back into the inbox. (In practice it crashed
        # first, on the relative_to() below, which masked the real hazard.)
        rows = ctx.conn.execute(
            """
            SELECT id, file_path, artist, album, title
            FROM archive
            WHERE status = 'CATALOGUED'
              AND artist IS NOT NULL
              AND title IS NOT NULL
              AND (finalized_at IS NULL OR finalized_at = '')
            ORDER BY file_path
            """
        ).fetchall()

        renamed = 0
        moved = 0
        skipped = 0

        for row in rows:
            result.files_processed += 1

            current_path = Path(row["file_path"])
            if not current_path.exists():
                logger.warning("[organize] file missing: %s", current_path)
                result.files_errored += 1
                result.errors.append(f"{current_path}: file missing on disk")
                continue

            # Second guard, independent of the finalized_at predicate above:
            # this stage only ever builds targets under ctx.inbox, so a row
            # living anywhere else (STAGING mid-canonicalize, ALAC-Library,
            # a hand-edited path) is not ours to reorganise. Skipping is
            # correct here, not an error.
            if not current_path.is_relative_to(ctx.inbox):
                skipped += 1
                continue

            artist = row["artist"] or "Unknown Artist"
            album = row["album"] or "Unsorted"
            title = row["title"] or "Unknown Title"

            # Build new filename
            ext = current_path.suffix
            new_filename = build_track_filename(artist, title, ext)

            # Build target path: INBOX/Artist/Album/filename
            artist_safe = sanitize_path_component(artist)
            album_safe = sanitize_path_component(album)

            target_dir = ctx.inbox / artist_safe / album_safe
            candidate_path = target_dir / new_filename

            # unique_path() checks disk existence to avoid collisions, but
            # the file being organized right now already exists at its
            # OWN current path -- if that happens to be the candidate
            # path (i.e. it's already correctly organized), unique_path()
            # would wrongly see that as "taken" and bump to " (2)".  Only
            # run collision-avoidance when the file is actually moving
            # somewhere new.
            if candidate_path == current_path:
                target_path = candidate_path
            else:
                target_path = unique_path(candidate_path)

            # Check if already organized
            if current_path == target_path:
                skipped += 1
                continue

            # Check if only needs rename (same directory)
            if current_path.parent == target_path.parent and current_path.name != target_path.name:
                # Just rename
                logger.info(
                    "[organize] rename  %s → %s",
                    current_path.name,
                    target_path.name,
                )

                if not dry_run and not self._apply_rename(
                    ctx, current_path, target_path, row["id"], "ORGANIZE_RENAME"
                ):
                    result.files_errored += 1
                    result.errors.append(f"{current_path.name}: DB collision, skipped")
                    continue

                renamed += 1
                result.files_changed += 1

            # Needs move (different directory)
            elif current_path.parent != target_path.parent:
                logger.info(
                    "[organize] move    %s\n                    → %s",
                    _display_path(current_path, ctx.inbox),
                    _display_path(target_path, ctx.inbox),
                )

                if not dry_run:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    if not self._apply_rename(
                        ctx, current_path, target_path, row["id"], "ORGANIZE_MOVE"
                    ):
                        result.files_errored += 1
                        result.errors.append(f"{current_path.name}: DB collision, skipped")
                        continue

                moved += 1
                result.files_changed += 1

            # Commit periodically
            if result.files_processed % _COMMIT_EVERY == 0 and not dry_run:
                ctx.conn.commit()
                logger.info("[organize] checkpoint %d", result.files_processed)

        prefix = "Would" if dry_run else "Done"
        result.notes.append(f"{prefix}: renamed={renamed}, moved={moved}, skipped={skipped}")

        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._organize(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._organize(ctx, dry_run=False)
