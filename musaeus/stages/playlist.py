#!/usr/bin/env python3
"""
MUSAEUS — Playlist Stage

Builds per-genre M3U8 playlists + All.m3u8 from the archive.

Source priority:
  1. car_export_path  — if curator has already run (recommended)
  2. file_path        — falls back to INBOX paths for standalone use

Output: vault_root/Playlists/<Genre>.m3u8 with relative paths and
        #EXTINF lines so playlists work on Android / Apple / Kodi
        regardless of where the USB drive mounts.

M3U8 format (portable):
    #EXTM3U
    #EXTINF:-1,Artist - Title
    ../Artist/Album/Track.m4a
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

from ..context import RunContext, StageResult
from .base import BaseStage

logger = logging.getLogger(__name__)

_GENRE_MAP: dict[str, str] = {
    "Disco/Electronic": "Disco-Electronic",
    "R&B/Funk/Soul": "R&B-Funk-Soul",
    "Folk Rock": "Folk_Rock",
    "Hard Rock": "Hard_Rock",
    "Hip Hop": "Hip_Hop",
    "Psychedelic Rock": "Psychedelic_Rock",
}


def _safe_genre(genre: str) -> str:
    genre = genre.strip()
    return _GENRE_MAP.get(genre, genre.replace("/", "-").replace(" ", "_"))


def _primary_genre(genre: str) -> str:
    """Take the first genre when a field contains comma-separated values."""
    return genre.split(",")[0].strip()


def _extinf_line(artist: str | None, title: str | None, source: str) -> str:
    """Build a #EXTINF:-1,Artist - Title line from DB metadata or filename."""
    if artist and title:
        return f"#EXTINF:-1,{artist.strip()} - {title.strip()}"
    # Fall back to filename stem
    stem = Path(source).stem
    if " - " in stem:
        parts = stem.split(" - ", 1)
        return f"#EXTINF:-1,{parts[0].strip()} - {parts[1].strip()}"
    return f"#EXTINF:-1,{stem}"


class PlaylistStage(BaseStage):
    """
    Build M3U8 playlists grouped by genre + one All.m3u8.

    Reads from archive WHERE status='CATALOGUED' AND genre IS NOT NULL.
    Uses car_export_path if available; falls back to file_path.

    Writes to vault_root/Playlists/:
        <Genre>.m3u8  — one per genre, relative paths, #EXTINF lines
        All.m3u8      — every matched track once, sorted
    """

    NAME = "playlist"

    def validate(self, ctx: RunContext) -> None:
        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' AND genre IS NOT NULL"
        ).fetchone()[0]
        logger.info("[playlist] %d catalogued files have genre", count)

    def _build(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        assert ctx.config is not None

        playlist_dir = ctx.config.vault_root / "Playlists"

        rows = ctx.conn.execute(
            """
            SELECT file_path, car_export_path, genre, artist, title
            FROM archive
            WHERE status = 'CATALOGUED'
              AND genre IS NOT NULL AND trim(genre) != ''
            ORDER BY genre, artist, title
            """
        ).fetchall()

        if not rows:
            result.notes.append("no catalogued files with genre — run Enrich first")
            ctx.record_stage(result)
            return result

        # Group by primary genre; collect (source, artist, title) tuples
        genre_tracks: dict[str, list[tuple[str, str | None, str | None]]] = defaultdict(list)
        all_sources: dict[str, tuple[str | None, str | None]] = {}  # source→(artist,title)
        no_source = 0

        for row in rows:
            source = row["car_export_path"] or row["file_path"]
            if not source:
                no_source += 1
                continue
            genre = _primary_genre(row["genre"])
            artist = row["artist"]
            title = row["title"]
            genre_tracks[genre].append((source, artist, title))
            all_sources[source] = (artist, title)

        result.notes.append(f"genres found: {len(genre_tracks)}")
        result.notes.append(
            f"track-genre assignments: {sum(len(v) for v in genre_tracks.values())}"
        )
        if no_source:
            result.notes.append(f"skipped (no source path): {no_source}")

        if not dry_run:
            playlist_dir.mkdir(parents=True, exist_ok=True)

        written = 0

        def _make_rel(source: str) -> str:
            """Compute relative path from playlist_dir to source file."""
            try:
                src_path = Path(source)
                # Try to make relative from playlist_dir
                rel = src_path.relative_to(playlist_dir.parent)
                return str(Path("..") / rel)
            except ValueError:
                # source is outside vault — use absolute as fallback
                return source

        # Per-genre playlists
        for genre, tracks in sorted(genre_tracks.items()):
            safe = _safe_genre(genre)
            out = playlist_dir / f"{safe}.m3u8"
            lines = ["#EXTM3U"]
            for source, artist, title in sorted(tracks, key=lambda t: t[0]):
                lines.append(_extinf_line(artist, title, source))
                lines.append(_make_rel(source))
            content = "\n".join(lines) + "\n"

            if dry_run:
                result.notes.append(f"  [DRY] {out.name}  ({len(tracks)} tracks)")
            else:
                out.write_text(content, encoding="utf-8")
                ctx.log_event(
                    "PLAYLIST_WRITTEN",
                    file_path=str(out),
                    new_value=f"{len(tracks)} tracks",
                    stage=self.NAME,
                )
                result.notes.append(f"  {out.name}  ({len(tracks)} tracks)")
                written += 1

            result.files_processed += len(tracks)
            result.files_changed += len(tracks)

        # All.m3u8 — every matched source once
        if all_sources:
            out_all = playlist_dir / "All.m3u8"
            all_lines = ["#EXTM3U"]
            for source in sorted(all_sources):
                artist, title = all_sources[source]
                all_lines.append(_extinf_line(artist, title, source))
                all_lines.append(_make_rel(source))
            all_content = "\n".join(all_lines) + "\n"
            n_all = len(all_sources)

            if dry_run:
                result.notes.append(f"  [DRY] All.m3u8  ({n_all} tracks)")
            else:
                out_all.write_text(all_content, encoding="utf-8")
                ctx.log_event(
                    "PLAYLIST_WRITTEN",
                    file_path=str(out_all),
                    new_value=f"{n_all} tracks",
                    stage=self.NAME,
                )
                result.notes.append(f"  All.m3u8  ({n_all} tracks)")
                written += 1

        if not dry_run:
            result.notes.append(f"playlists written: {written}")

        ctx.record_stage(result)
        return result

    def run(self, ctx: RunContext) -> StageResult:
        return self._build(ctx, dry_run=False)

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._build(ctx, dry_run=True)
