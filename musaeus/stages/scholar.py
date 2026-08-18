#!/usr/bin/env python3
"""
MUSAEUS — Stage 3: Scholar
Extract full metadata from audio files via ffprobe.

What it does:
  - Processes archive rows with status='HASHED' (post-Sentinel)
  - Runs ffprobe -print_format json to extract stream + tag data
  - Populates archive and metadata_cache with: title, artist, album, genre,
    year, track, duration, bitrate (int!), sample_rate, channels, codec
  - Advances status to 'CATALOGUED'
  - Logs METADATA_EXTRACTED event per file
  - dry_run() reports the count and sample without any DB writes

Design:
  - bitrate is always stored as INTEGER (NexusII bug N12 avoided)
  - All tag lookups are case-insensitive (ffprobe can capitalise inconsistently)
  - Scholar never touches file content — read-only against the audio files
  - raw_json is stored in metadata_cache for full audit trail
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from ..context import RunContext, StageResult
from ..db import upsert_archive
from .base import BaseStage

logger = logging.getLogger(__name__)

_FFPROBE_CMD = "ffprobe"


# ── ffprobe wrapper ───────────────────────────────────────────────────────────


class ProbeError(Exception):
    """ffprobe returned an error or couldn't be found."""


def _probe(path: Path) -> dict[str, Any]:
    """
    Run ffprobe and return the parsed JSON dict.
    Raises ProbeError on failure.
    """
    cmd = [
        _FFPROBE_CMD,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise ProbeError("ffprobe not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe timed out for {path.name}") from exc

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ProbeError(f"ffprobe exit {proc.returncode}: {stderr[:200]}")

    try:
        data: dict[str, Any] = json.loads(proc.stdout)
        return data
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe JSON parse error for {path.name}") from exc


def _extract_meta(probe_data: dict[str, Any]) -> dict[str, Any]:
    """
    Parse ffprobe JSON into a flat metadata dict.
    Returns safe defaults for all fields so callers never get KeyError.
    """
    fmt = probe_data.get("format", {})
    streams = probe_data.get("streams", [])

    # Find first audio stream
    audio_stream: dict[str, Any] = {}
    for s in streams:
        if s.get("codec_type") == "audio":
            audio_stream = s
            break

    # Tags can be in format or stream; format takes precedence
    fmt_tags: dict[str, str] = {k.lower(): v for k, v in fmt.get("tags", {}).items()}
    stream_tags: dict[str, str] = {k.lower(): v for k, v in audio_stream.get("tags", {}).items()}
    tags = {**stream_tags, **fmt_tags}

    def tag(*keys: str) -> str | None:
        for k in keys:
            v = tags.get(k.lower())
            if v:
                return v.strip()
        return None

    # Duration: prefer format-level (more reliable)
    duration_str = fmt.get("duration") or audio_stream.get("duration")
    try:
        duration = float(duration_str) if duration_str else None
    except (ValueError, TypeError):
        duration = None

    # Bitrate: format-level is whole-file average; stream-level is more precise
    def _int_or_none(val: Any) -> int | None:
        try:
            return int(float(str(val))) if val else None
        except (ValueError, TypeError):
            return None

    bitrate = _int_or_none(audio_stream.get("bit_rate") or fmt.get("bit_rate"))
    sample_rate = _int_or_none(audio_stream.get("sample_rate"))
    channels = _int_or_none(audio_stream.get("channels"))

    # Track number: "5/12" → 5
    track_raw = tag("track", "tracknumber")
    track: int | None = None
    if track_raw:
        with contextlib.suppress(ValueError):
            track = int(track_raw.split("/")[0].strip())

    # Year: date tag commonly "YYYY" or "YYYY-MM-DD"
    year = tag("date", "year", "originaldate")
    if year and len(year) > 4:
        year = year[:4]

    return {
        "title": tag("title"),
        "artist": tag("artist", "albumartist"),
        "album": tag("album"),
        "genre": tag("genre"),
        "year": year,
        "track": track,
        "duration": duration,
        "bitrate": bitrate,
        "sample_rate": sample_rate,
        "channels": channels,
        "codec": audio_stream.get("codec_name"),
    }


# ── Stage ─────────────────────────────────────────────────────────────────────


def _get_hashed(conn) -> list[dict]:  # type: ignore[type-arg]
    """Return archive rows ready for Scholar (HASHED status)."""
    rows = conn.execute(
        "SELECT file_path FROM archive WHERE status='HASHED' ORDER BY file_path"
    ).fetchall()
    return [dict(r) for r in rows]


class ScholarStage(BaseStage):
    """
    Stage 3 — Extract and store audio metadata via ffprobe.
    """

    NAME = "scholar"

    # ── Validate ──────────────────────────────────────────────────────────────

    def validate(self, ctx: RunContext) -> None:
        count = ctx.conn.execute("SELECT COUNT(*) FROM archive WHERE status='HASHED'").fetchone()[0]
        if count == 0:
            logger.info("[scholar] no HASHED files — stage will be a no-op")

    # ── Dry run ───────────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=True)
        pending = _get_hashed(ctx.conn)
        result.files_processed = len(pending)
        result.files_changed = len(pending)
        result.notes.append(f"Would probe {len(pending)} HASHED file(s).")
        for row in pending[:10]:
            result.notes.append(f"  ~ {Path(row['file_path']).name}")
        if len(pending) > 10:
            result.notes.append(f"  ... and {len(pending) - 10} more")
        ctx.record_stage(result)
        return result

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)
        pending = _get_hashed(ctx.conn)

        _COMMIT_EVERY = 50  # commit progress incrementally

        for row in pending:
            path_str = row["file_path"]
            path = Path(path_str)
            result.files_processed += 1

            if not path.exists():
                result.files_errored += 1
                result.errors.append(f"Missing: {path_str}")
                logger.warning("file gone: %s", path_str)
                continue

            try:
                probe_data = _probe(path)
            except ProbeError as exc:
                result.files_errored += 1
                result.errors.append(f"{path.name}: {exc}")
                logger.warning("probe failed: %s — %s", path.name, exc)
                continue

            meta = _extract_meta(probe_data)
            raw_json = json.dumps(probe_data, ensure_ascii=False)

            # Update archive
            archive_row = {"file_path": path_str, "status": "CATALOGUED", **meta}
            upsert_archive(ctx.conn, archive_row)

            # Update metadata_cache with raw JSON for full audit trail
            ctx.conn.execute(
                """
                INSERT INTO metadata_cache
                    (file_path, title, artist, album, genre, year, track,
                     duration, bitrate, sample_rate, channels, codec, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_path) DO UPDATE SET
                    title=excluded.title, artist=excluded.artist,
                    album=excluded.album, genre=excluded.genre,
                    year=excluded.year, track=excluded.track,
                    duration=excluded.duration, bitrate=excluded.bitrate,
                    sample_rate=excluded.sample_rate, channels=excluded.channels,
                    codec=excluded.codec, raw_json=excluded.raw_json,
                    scanned_at=datetime('now')
                """,
                (
                    path_str,
                    meta["title"],
                    meta["artist"],
                    meta["album"],
                    meta["genre"],
                    meta["year"],
                    meta["track"],
                    meta["duration"],
                    meta["bitrate"],
                    meta["sample_rate"],
                    meta["channels"],
                    meta["codec"],
                    raw_json,
                ),
            )

            ctx.log_event(
                "METADATA_EXTRACTED",
                file_path=path_str,
                stage=self.NAME,
                note=(
                    f"artist={meta.get('artist')!r} "
                    f"title={meta.get('title')!r} "
                    f"bitrate={meta.get('bitrate')}"
                ),
            )
            result.files_changed += 1
            logger.info(
                "catalogued: %s — %s / %s",
                path.name,
                meta.get("artist", "?"),
                meta.get("title", "?"),
            )

            # Periodic commit so progress survives a crash
            if result.files_processed % _COMMIT_EVERY == 0:
                ctx.conn.commit()
                logger.info(
                    "[scholar] checkpoint %d / %d",
                    result.files_processed,
                    len(pending),
                )

        if result.files_errored > 0:
            result.success = False

        ctx.record_stage(result)
        return result
