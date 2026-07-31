#!/usr/bin/env python3
"""
MUSAEUS — Stage: Playlist
Build genre-based and energy-based M3U8 playlists from the archive.

What it does:
  - Queries archive for all CATALOGUED+ tracks (not GHOST, not PENDING)
  - Groups by genre → writes one .m3u8 per genre
  - Builds energy-based playlists if archive has bpm/energy data
  - Output directory: cfg.runs_root / "Playlists"
  - M3U8 format: #EXTM3U header, #EXTINF lines, file paths
  - Minimum 3 tracks per genre to create a playlist
  - Sorts tracks by artist, album, track number within each playlist

Requirements:
  - No external deps (pure Python file writing)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

from ..config import get_config
from ..context import RunContext, StageResult
from ..db import log_event
from .base import BaseStage, StageError

logger = logging.getLogger(__name__)

_MIN_TRACKS_PER_GENRE = 3


class PlaylistStage(BaseStage):
    """
    Playlist — build genre-based and energy-based M3U8 playlists.
    """

    NAME = "playlist"

    # ── Validate ──────────────────────────────────────────────────────────────

    def validate(self, ctx: RunContext) -> None:
        count = ctx.conn.execute(
            """
            SELECT COUNT(*) FROM archive
            WHERE status NOT IN ('GHOST', 'PENDING')
            """
        ).fetchone()[0]
        if count == 0:
            raise StageError("no catalogued tracks in archive — nothing to build playlists from")
        logger.info("[playlist] %d track(s) available for playlists", count)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_output_dir(self, ctx: RunContext) -> Path:
        """Determine the playlist output directory."""
        cfg = get_config()
        return cfg.runs_root / "Playlists"

    def _get_tracks(self, ctx: RunContext) -> list[dict]:
        """Return all eligible tracks for playlisting."""
        rows = ctx.conn.execute(
            """
            SELECT file_path, artist, album, title, genre, track, duration
            FROM archive
            WHERE status NOT IN ('GHOST', 'PENDING')
            ORDER BY artist, album, track
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def _has_energy_column(self, ctx: RunContext) -> bool:
        """Check if the archive has an energy or bpm column."""
        cols = {
            row[1]
            for row in ctx.conn.execute("PRAGMA table_info(archive)").fetchall()
        }
        return "energy" in cols or "bpm" in cols

    def _get_energy_tracks(self, ctx: RunContext) -> list[dict]:
        """Return tracks with energy/bpm data for energy-based playlists."""
        cols = {
            row[1]
            for row in ctx.conn.execute("PRAGMA table_info(archive)").fetchall()
        }

        if "bpm" in cols:
            rows = ctx.conn.execute(
                """
                SELECT file_path, artist, album, title, genre, track, duration, bpm
                FROM archive
                WHERE status NOT IN ('GHOST', 'PENDING')
                  AND bpm IS NOT NULL AND bpm > 0
                ORDER BY bpm
                """
            ).fetchall()
            return [dict(r) for r in rows]
        elif "energy" in cols:
            rows = ctx.conn.execute(
                """
                SELECT file_path, artist, album, title, genre, track, duration, energy
                FROM archive
                WHERE status NOT IN ('GHOST', 'PENDING')
                  AND energy IS NOT NULL
                ORDER BY energy
                """
            ).fetchall()
            return [dict(r) for r in rows]
        return []

    def _write_m3u8(self, path: Path, tracks: list[dict]) -> None:
        """Write an M3U8 playlist file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for t in tracks:
                duration = int(t.get("duration") or -1)
                artist = t.get("artist") or "Unknown"
                title = t.get("title") or "Unknown"
                f.write(f"#EXTINF:{duration},{artist} - {title}\n")
                f.write(f"{t['file_path']}\n")

    def _sort_tracks(self, tracks: list[dict]) -> list[dict]:
        """Sort tracks by artist, album, track number."""
        def sort_key(t: dict) -> tuple:
            return (
                (t.get("artist") or "").lower(),
                (t.get("album") or "").lower(),
                t.get("track") or 0,
            )
        return sorted(tracks, key=sort_key)

    def _sanitize_filename(self, name: str) -> str:
        """Make a genre name safe for use as a filename."""
        safe = "".join(c for c in name if c not in '<>:"/\\|?*').strip()
        return safe or "Unknown"

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)
        output_dir = self._get_output_dir(ctx)
        output_dir.mkdir(parents=True, exist_ok=True)

        tracks = self._get_tracks(ctx)
        result.notes.append(f"total tracks: {len(tracks)}")

        # ── Genre-based playlists ─────────────────────────────────────────────
        genre_groups: dict[str, list[dict]] = defaultdict(list)
        for t in tracks:
            genre = (t.get("genre") or "").strip()
            if genre:
                genre_groups[genre].append(t)

        playlists_created = 0
        for genre, genre_tracks in sorted(genre_groups.items()):
            if len(genre_tracks) < _MIN_TRACKS_PER_GENRE:
                continue

            sorted_tracks = self._sort_tracks(genre_tracks)
            safe_name = self._sanitize_filename(genre)
            playlist_path = output_dir / f"{safe_name}.m3u8"
            self._write_m3u8(playlist_path, sorted_tracks)
            playlists_created += 1
            result.files_changed += 1

        result.notes.append(f"genre playlists created: {playlists_created}")

        # ── Energy-based playlists ────────────────────────────────────────────
        energy_playlists = 0
        if self._has_energy_column(ctx):
            energy_tracks = self._get_energy_tracks(ctx)
            if energy_tracks:
                # Split into low/mid/high energy tiers
                n = len(energy_tracks)
                tier_size = n // 3

                if tier_size >= _MIN_TRACKS_PER_GENRE:
                    tiers = {
                        "Low Energy": energy_tracks[:tier_size],
                        "Mid Energy": energy_tracks[tier_size:2 * tier_size],
                        "High Energy": energy_tracks[2 * tier_size:],
                    }

                    for tier_name, tier_tracks in tiers.items():
                        if len(tier_tracks) >= _MIN_TRACKS_PER_GENRE:
                            playlist_path = output_dir / f"{tier_name}.m3u8"
                            self._write_m3u8(playlist_path, tier_tracks)
                            energy_playlists += 1
                            result.files_changed += 1

        if energy_playlists:
            result.notes.append(f"energy playlists created: {energy_playlists}")
        else:
            result.notes.append("energy playlists: skipped (no bpm/energy data)")

        total_playlists = playlists_created + energy_playlists
        result.files_processed = len(tracks)

        # Log event
        log_event(
            ctx.conn,
            run_id=ctx.run_id,
            event_type="PLAYLISTS_BUILT",
            stage=self.NAME,
            note=f"count={total_playlists}",
        )

        ctx.record_stage(result)
        return result

    # ── Dry run ───────────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=True)
        output_dir = self._get_output_dir(ctx)

        tracks = self._get_tracks(ctx)
        result.files_processed = len(tracks)

        # ── Genre breakdown ───────────────────────────────────────────────────
        genre_groups: dict[str, int] = defaultdict(int)
        for t in tracks:
            genre = (t.get("genre") or "").strip()
            if genre:
                genre_groups[genre] += 1

        would_create = []
        for genre, count in sorted(genre_groups.items()):
            if count >= _MIN_TRACKS_PER_GENRE:
                would_create.append((genre, count))

        result.notes.append(f"[DRY RUN] output directory: {output_dir}")
        result.notes.append(f"[DRY RUN] total eligible tracks: {len(tracks)}")
        result.notes.append(f"[DRY RUN] genre playlists to create: {len(would_create)}")
        for genre, count in would_create[:10]:
            result.notes.append(f"    {genre}: {count} tracks")
        if len(would_create) > 10:
            result.notes.append(f"    ... and {len(would_create) - 10} more")

        # Energy playlists
        if self._has_energy_column(ctx):
            energy_tracks = self._get_energy_tracks(ctx)
            if energy_tracks:
                result.notes.append(
                    f"[DRY RUN] energy playlists: would create up to 3 "
                    f"(from {len(energy_tracks)} tracks with bpm/energy)"
                )
            else:
                result.notes.append("[DRY RUN] energy playlists: no tracks with bpm/energy data")
        else:
            result.notes.append("[DRY RUN] energy playlists: skipped (no bpm/energy column)")

        ctx.record_stage(result)
        return result
