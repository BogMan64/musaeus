#!/usr/bin/env python3
"""
MUSAEUS — Playlist Stage

Builds per-genre M3U8 playlists from the archive.

Source priority:
  1. car_export_path  — if curator has already run (recommended)
  2. file_path        — falls back to INBOX paths for standalone use

Output: vault_root/Playlists/<Genre>.m3u8 with absolute paths.
"""

from __future__ import annotations

import logging
from collections import defaultdict

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


class PlaylistStage(BaseStage):
    """
    Build M3U8 playlists grouped by genre.

    Reads from archive WHERE status='CATALOGUED' AND genre IS NOT NULL.
    Uses car_export_path if available; falls back to file_path.
    Writes to vault_root/Playlists/<Genre>.m3u8.
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

        # Group by primary genre
        genre_tracks: dict[str, list[str]] = defaultdict(list)
        no_source = 0
        for row in rows:
            source = row["car_export_path"] or row["file_path"]
            if not source:
                no_source += 1
                continue
            genre = _primary_genre(row["genre"])
            genre_tracks[genre].append(source)

        result.notes.append(f"genres found: {len(genre_tracks)}")
        result.notes.append(
            f"track-genre assignments: {sum(len(v) for v in genre_tracks.values())}"
        )
        if no_source:
            result.notes.append(f"skipped (no source path): {no_source}")

        if not dry_run:
            playlist_dir.mkdir(parents=True, exist_ok=True)

        written = 0
        for genre, paths in sorted(genre_tracks.items()):
            safe = _safe_genre(genre)
            out = playlist_dir / f"{safe}.m3u8"
            lines = ["#EXTM3U"] + sorted(paths)
            content = "\n".join(lines) + "\n"

            if dry_run:
                result.notes.append(f"  [DRY] {out.name}  ({len(paths)} tracks)")
            else:
                out.write_text(content, encoding="utf-8")
                ctx.log_event(
                    "PLAYLIST_WRITTEN",
                    file_path=str(out),
                    new_value=f"{len(paths)} tracks",
                    stage=self.NAME,
                )
                result.notes.append(f"  {out.name}  ({len(paths)} tracks)")
                written += 1

            result.files_processed += len(paths)
            result.files_changed += len(paths)

        if not dry_run:
            result.notes.append(f"playlists written: {written}")

        ctx.record_stage(result)
        return result

    def run(self, ctx: RunContext) -> StageResult:
        return self._build(ctx, dry_run=False)

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._build(ctx, dry_run=True)
