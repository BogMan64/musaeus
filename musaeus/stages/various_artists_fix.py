#!/usr/bin/env python3
"""
MUSAEUS — Various Artists Fix Stage (standalone, not wired into DEFAULT_PIPELINE)

Resolves the real artist for tracks tagged "Various Artists" and
corrects both archive.artist and the file's physical location/name.
Ported from ORPHEUS's fix_various_artists.py, per tonight's 222-script
ORPHEUS salvage audit. Standalone, matching the Ghost/Permissions/BPM/
TributeQuarantine precedent.

`fix_various_artist_filenames.py`, the second script the audit initially
flagged alongside this one, turned out to be incomplete when actually
read in full: its main() sets up argument parsing and dry-run/apply
handling but never calls any of its own read_tags/is_various/
title_from_filename helpers -- the scan-and-rename logic is simply
missing, not something to port. Its intended job (rename+route
already-misplaced Various-Artists-prefixed files) is already covered by
this script's own file-relocation behavior anyway (ORPHEUS's --move
flag, ported below), so nothing is actually lost by treating this as one
script, not two.

Design differences from the ORPHEUS original:
  - DB-row-driven (archive table, status='CATALOGUED'), not a directory
    walk.
  - Strategies ported: bracket extraction ("[Real Artist]" in filename),
    filename-segment splitting ("Various Artists - Real Artist -
    Title"), MusicBrainz recording lookup (optional, matching the
    original's --no-mb). Dropped: ORPHEUS's own "track artist tag"
    strategy, which is dead code in the original -- its condition
    (tags["artist"] and not is_various(tags["artist"])) can never be
    true for a row that only became a candidate because
    is_various(tags["artist"]) was already True. Dropped "album artist
    tag" too -- MUSAEUS's archive table has no albumartist column, and
    unlike bracket/filename-segment extraction (which read straight
    from the filename, already available), resolving this one would
    mean a real file-tag read for a strategy the original itself only
    used as a lower-priority fallback.
  - A resolved row is moved into the corrected Artist/Unsorted location
    (same ALAC-Library/<batch-date>/Artist/Album convention
    FinalizeStage itself uses -- reusing the row's own existing
    batch-date folder rather than stamping a new one) and
    archive.file_path is updated to match. Keeps DB and disk in sync
    rather than leaving a corrected archive.artist pointing at a stale
    Various-Artists path -- the same stale-file_path bug class fixed
    elsewhere tonight.
  - Rows nothing can resolve are left CATALOGUED with archive.artist
    still "Various Artists" -- reported for manual review, never
    guessed at.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
import urllib.error
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..context import RunContext, StageResult
from .base import BaseStage
from .organize import build_track_filename, sanitize_path_component, unique_path

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 25

VARIOUS_ARTISTS_FORMS = frozenset(
    {
        "various artists",
        "various",
        "va",
        "various artist",
    }
)

# "Artist - Title [Real Artist]" or "Artist - Title [Real Artist-Label]"
_BRACKET_RE = re.compile(r"\[([^\]]+)\]")

_MB_BASE = "https://musicbrainz.org/ws/2"
_USER_AGENT = "Musaeus/1.0 (music-library-manager; contact@musaeus.local)"
_MB_RATE_LIMIT_S = 1.1  # MB requires <= 1 req/s for unauthenticated access
_MB_TIMEOUT_S = 15


def is_various(artist: str) -> bool:
    return (artist or "").strip().lower() in VARIOUS_ARTISTS_FORMS


def extract_from_brackets(filename: str) -> str:
    """Extract artist from a "[Artist]" or "[Artist-Label]" bracket in the
    filename. "The Ronettes-Phil Spector" -> "The Ronettes" (heuristic:
    if the part before the dash is long enough to plausibly be a full
    name on its own, prefer it over the whole bracket contents)."""
    stem = Path(filename).stem
    m = _BRACKET_RE.search(stem)
    if not m:
        return ""
    content = m.group(1)
    if "-" in content:
        parts = [p.strip() for p in content.split("-")]
        if len(parts[0]) > 3:
            return parts[0]
    return content.strip()


def extract_from_filename_segments(filename: str) -> str:
    """ "Various Artists - Real Artist - Song Title.ext" -> "Real Artist".
    Splits on " - "; if there are at least 3 segments and the first is a
    Various Artists marker, the second segment is the real artist."""
    stem = Path(filename).stem
    parts = [p.strip() for p in stem.split(" - ")]
    if len(parts) >= 3 and is_various(parts[0]):
        return parts[1]
    return ""


def _lookup_musicbrainz(title: str, album: str = "") -> str:
    """Query MusicBrainz's recording search for the credited artist of a
    track by title (+ album, if known). Returns "" on any failure --
    never raises, matching every other best-effort MB call in this
    project."""
    try:
        query = f'recording:"{title}"'
        if album:
            query += f' AND release:"{album}"'
        url = f"{_MB_BASE}/recording?" + urlencode({"query": query, "fmt": "json", "limit": "1"})
        req = Request(url, headers={"User-Agent": _USER_AGENT})
        with urlopen(req, timeout=_MB_TIMEOUT_S) as resp:  # nosec B310
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        logger.debug("[various-artists-fix] MB lookup failed for %r: %s", title, exc)
        return ""

    recordings = data.get("recordings", [])
    if not recordings:
        return ""
    credits = recordings[0].get("artist-credit", [])
    if not credits:
        return ""
    result: str = credits[0].get("artist", {}).get("name", "")
    return result


def find_real_artist(source: Path, title: str, album: str, use_mb: bool) -> tuple[str, str]:
    """Returns (real_artist, strategy). real_artist is "" if unresolved."""
    filename = source.name

    artist = extract_from_brackets(filename)
    if artist and not is_various(artist):
        return artist, "bracket"

    artist = extract_from_filename_segments(filename)
    if artist and not is_various(artist):
        return artist, "filename_segment"

    if use_mb and title:
        time.sleep(_MB_RATE_LIMIT_S)
        artist = _lookup_musicbrainz(title, album)
        if artist and not is_various(artist):
            return artist, "musicbrainz"

    return "", "unknown"


class VariousArtistsFixStage(BaseStage):
    """
    Resolve the real artist for CATALOGUED rows tagged "Various Artists",
    correct archive.artist, and relocate the file into the corrected
    Artist/Unsorted location. Standalone -- not part of DEFAULT_PIPELINE.
    Use ctx.set("various_artists_no_mb", True) to skip the MusicBrainz
    lookup strategy (faster, offline-safe, matching the original's
    --no-mb).
    """

    NAME = "various-artists-fix"

    def validate(self, ctx: RunContext) -> None:
        """No external dependency to check -- pure Python pattern
        matching, MusicBrainz lookup is optional and best-effort."""

    def _get_candidates(self, ctx: RunContext) -> list[dict]:
        rows = ctx.conn.execute(
            "SELECT id, file_path, artist, title, album FROM archive WHERE status='CATALOGUED'"
        ).fetchall()
        return [dict(r) for r in rows if is_various(r["artist"])]

    def _batch_date_for(self, ctx: RunContext, source: Path) -> str:
        """Reuse the row's own existing batch-date folder
        (ALAC-Library/<date>/...) rather than stamping a new one --
        this is a correction to an already-finalized row, not a new
        finalize event."""
        try:
            rel = source.relative_to(ctx.alac_library)
            if rel.parts:
                return rel.parts[0]
        except ValueError:
            pass
        from datetime import datetime, timezone

        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    def _target_path(self, ctx: RunContext, row: dict, source: Path, real_artist: str) -> Path:
        album = row.get("album") or "Unsorted"
        title = row.get("title") or "Unknown Title"

        new_filename = build_track_filename(real_artist, title, source.suffix)
        artist_safe = sanitize_path_component(real_artist)
        album_safe = sanitize_path_component(album)

        target_dir = ctx.alac_library / self._batch_date_for(ctx, source) / artist_safe / album_safe
        return unique_path(target_dir / new_filename)

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)
        use_mb = not ctx.get("various_artists_no_mb", False)
        candidates = self._get_candidates(ctx)

        result.notes.append(f"candidates: {len(candidates)}")
        if not candidates:
            result.notes.append("nothing to do — no Various Artists rows found")
            ctx.record_stage(result)
            return result

        fixed = 0
        unknown = 0
        for i, row in enumerate(candidates, 1):
            result.files_processed += 1
            source = Path(row["file_path"])
            if not source.exists():
                result.files_errored += 1
                result.errors.append(f"{source}: file missing on disk")
                continue

            real_artist, strategy = find_real_artist(
                source, row.get("title") or "", row.get("album") or "", use_mb
            )
            if not real_artist:
                unknown += 1
                result.files_skipped += 1
                continue

            target = self._target_path(ctx, row, source, real_artist)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
            except OSError as exc:
                result.files_errored += 1
                result.errors.append(f"{source.name}: {exc}")
                continue

            ctx.conn.execute(
                "UPDATE archive SET artist = ?, file_path = ? WHERE id = ?",
                (real_artist, str(target), row["id"]),
            )
            ctx.log_event(
                "VARIOUS_ARTISTS_FIXED",
                file_path=str(target),
                old_value=str(source),
                new_value=str(target),
                stage=self.NAME,
                note=f"{row.get('artist')} -> {real_artist} (via {strategy})",
            )
            fixed += 1
            result.files_changed += 1
            logger.info(
                "[various-artists-fix] %s -> %s (%s)", row.get("artist"), real_artist, strategy
            )

            if i % _COMMIT_EVERY == 0:
                ctx.conn.commit()

        ctx.conn.commit()

        result.notes.append(f"fixed: {fixed}")
        result.notes.append(f"unknown (left as Various Artists): {unknown}")
        if result.files_errored:
            result.success = False

        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=True)
        use_mb = not ctx.get("various_artists_no_mb", False)
        candidates = self._get_candidates(ctx)

        would_fix = 0
        for row in candidates:
            source = Path(row["file_path"])
            if not source.exists():
                continue
            real_artist, strategy = find_real_artist(
                source, row.get("title") or "", row.get("album") or "", use_mb
            )
            if real_artist:
                would_fix += 1
                result.notes.append(f"  {row.get('artist')} -> {real_artist}  (via {strategy})")

        result.files_processed = len(candidates)
        result.notes.insert(0, f"[DRY RUN] {len(candidates)} candidate(s), would fix {would_fix}")
        result.notes.append("  no files moved, no DB changes")

        ctx.record_stage(result)
        return result
