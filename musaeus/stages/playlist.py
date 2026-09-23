#!/usr/bin/env python3
"""
MUSAEUS — Playlist Stage

Builds per-genre M3U8 playlists, per-decade Era_*.m3u8 lists, and
All.m3u8 from the archive.

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


def _decade(year: str | None) -> str | None:
    """Map a 4-digit year to its decade label ('1967' -> '1960s').

    Returns None for missing or non-numeric years rather than guessing —
    an era list built on a fabricated year is worse than a shorter list.
    """
    if not year:
        return None
    y = year.strip()[:4]
    if len(y) != 4 or not y.isdigit():
        return None
    return f"{y[:3]}0s"


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

        playlist_dir = ctx.config.playlists

        rows = ctx.conn.execute(
            """
            SELECT file_path, car_export_path, genre, artist, title, year
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
        decade_tracks: dict[str, list[tuple[str, str | None, str | None]]] = defaultdict(list)
        no_year = 0
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

            decade = _decade(row["year"])
            if decade:
                decade_tracks[decade].append((source, artist, title))
            else:
                no_year += 1

        result.notes.append(f"genres found: {len(genre_tracks)}")
        result.notes.append(
            f"track-genre assignments: {sum(len(v) for v in genre_tracks.values())}"
        )
        result.notes.append(f"decades found: {len(decade_tracks)}")
        if no_source:
            result.notes.append(f"skipped (no source path): {no_source}")
        if no_year:
            result.notes.append(f"no era list (missing/invalid year): {no_year}")

        if not dry_run:
            playlist_dir.mkdir(parents=True, exist_ok=True)

        written = 0

        # A set, not a list: every source is offered to _make_rel once per
        # genre pass, once per era pass and once for All, so a list would
        # report roughly 2.5x the number of files actually skipped.
        outside: set[str] = set()

        def _make_rel(source: str) -> str | None:
            """Relative path from playlist_dir to source, or None if outside.

            Returning the absolute path for an outside source -- which this
            did until 2026-09-17 -- defeats the entire point of the file.
            The docstring says these playlists work "regardless of where the
            USB drive mounts"; one absolute line pointing into the vault is
            a dead entry the moment the playlist is read anywhere else, and
            it reads as a working line to every player that opens it.

            It matters most for an edition index. Pointed at
            CAR_Library/Playlists, a row with no car_export_path falls back
            to its ALAC_Library path, which is outside that tree -- so every
            un-exported track would have shipped to the car USB as an
            absolute /mnt/FORGE2TB/... line. Skipped and counted instead.
            """
            try:
                rel = Path(source).relative_to(playlist_dir.parent)
            except ValueError:
                outside.add(source)
                return None
            return str(Path("..") / rel)

        # Per-genre playlists
        for genre, tracks in sorted(genre_tracks.items()):
            safe = _safe_genre(genre)
            out = playlist_dir / f"{safe}.m3u8"
            lines = ["#EXTM3U"]
            for source, artist, title in sorted(tracks, key=lambda t: t[0]):
                rel = _make_rel(source)
                if rel is None:
                    continue
                lines.append(_extinf_line(artist, title, source))
                lines.append(rel)
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

        # Per-decade era playlists. These re-list tracks already counted by the
        # genre pass, so files_processed/files_changed are deliberately not
        # incremented again — the counts stay a count of files, not of lines.
        for decade, tracks in sorted(decade_tracks.items()):
            out = playlist_dir / f"Era_{decade}.m3u8"
            lines = ["#EXTM3U"]
            for source, artist, title in sorted(tracks, key=lambda t: t[0]):
                rel = _make_rel(source)
                if rel is None:
                    continue
                lines.append(_extinf_line(artist, title, source))
                lines.append(rel)
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

        # All.m3u8 — every matched source once
        if all_sources:
            out_all = playlist_dir / "All.m3u8"
            all_lines = ["#EXTM3U"]
            n_all = 0
            for source in sorted(all_sources):
                rel = _make_rel(source)
                if rel is None:
                    continue
                artist, title = all_sources[source]
                all_lines.append(_extinf_line(artist, title, source))
                all_lines.append(rel)
                n_all += 1
            all_content = "\n".join(all_lines) + "\n"

            if not n_all:
                pass
            elif dry_run:
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

        if outside:
            result.notes.append(
                f"skipped (path outside {playlist_dir.parent}, would need an "
                f"absolute line): {len(outside)}"
            )
        if not dry_run:
            result.notes.append(f"playlists written: {written}")

        ctx.record_stage(result)
        return result

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """A playlist this stage says it wrote must exist and have content.

        The event records the path and the track count. A write that
        failed, or produced an empty file because the track query returned
        nothing, leaves the count in the log and nothing on disk.
        """
        rows = ctx.conn.execute(
            "SELECT file_path FROM events WHERE run_id = ? "
            " AND event_type = 'PLAYLIST_WRITTEN' ORDER BY id DESC LIMIT 10",
            (ctx.run_id,),
        ).fetchall()
        if not rows:
            return []
        problems: list[str] = []
        for r in rows:
            p = Path(r["file_path"])
            if not p.exists():
                problems.append(f"playlist was not written: {p.name}")
            elif p.stat().st_size == 0:
                problems.append(f"playlist is empty: {p.name}")
        return problems

    def run(self, ctx: RunContext) -> StageResult:
        return self._build(ctx, dry_run=False)

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._build(ctx, dry_run=True)
