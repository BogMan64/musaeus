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
import re
import subprocess
from pathlib import Path
from typing import Any

from ..context import RunContext, StageResult, elision
from ..db import upsert_archive
from .base import BaseStage
from .organize import strip_track_number_prefix

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


# ── Names from the file name ──────────────────────────────────────────────────

# "5. " / "90. " / "573. " -- a playlist position, dotted. organize's own
# strip_track_number_prefix handles every other track-number shape ("03 - ",
# "01 ", "05.", "Disc 1 - 05 - ") and is used below rather than copied: the
# first version kept its own rule and read "03 - Yesterday" as an artist
# called "03" (cloud review of #37, 2026-09-25). The price is the few artists
# whose name IS a number -- 311, and 10,000 Maniacs / 98 Degrees as they arrive
# cut short. An untagged file from one of them is left UNNAMED for a human,
# which Scholar's own check reports, rather than given a wrong name.
_POSITION_RE = re.compile(r"^\d+\.\s+")
# " (2)" -- a collision suffix unique_path added, not part of the song. Two
# digits at most: "(1999)" is the song's.
_COLLISION_RE = re.compile(r" \((?:[2-9]|[1-9]\d)\)$")
# What organize, dupe_resolver and tribute_quarantine write for a row with no
# name. Reading one back as a name would make the placeholder real.
_PLACEHOLDERS = frozenset({"unknown artist", "unknown title", "unknown"})


def _clean_stem(path: Path) -> str:
    """The file name with track numbers and a collision suffix taken off."""
    stem = strip_track_number_prefix(_POSITION_RE.sub("", path.stem))
    return _COLLISION_RE.sub("", stem).strip()


def title_hint(path: Path) -> str | None:
    """What the file name still says about the title, or None.

    For a file _name_from_file could not name: "03 - Yesterday" -> "Yesterday".
    acoustid_name.py only accepts an AcoustID recording that agrees with it,
    because an AcoustID result is a cluster of recordings in no meaningful
    order, often polluted. No hint -- an empty or all-digit stem, or the
    pipeline's own placeholder -- means no name is taken at all.
    """
    stem = _clean_stem(path)
    if " - " in stem:
        stem = stem.partition(" - ")[2].strip()
    if not stem or stem.isdigit() or stem.casefold() in _PLACEHOLDERS:
        return None
    return stem


def _name_from_file(path: Path) -> tuple[str | None, str | None]:
    """(artist, title) from an "Artist - Title" file name, or (None, None).

    Only for a file with NO artist tag and NO title tag (2026-09-25: two
    Backstreet Boys files arrived like that and were catalogued blank,
    though their names said exactly what they were). Splits on the FIRST
    " - " only, so "Dion - Runaround Sue - Live" keeps " - Live" in the title.
    """
    stem = _clean_stem(path)
    artist, sep, title = stem.partition(" - ")
    artist, title = artist.strip(), title.strip()
    if not sep or not artist or not title:
        return None, None
    if artist.casefold() in _PLACEHOLDERS or title.casefold() in _PLACEHOLDERS:
        return None, None
    return artist, title


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
            result.notes.append(f"  {elision(len(pending) - 10)}")
        ctx.record_stage(result)
        return result

    # ── Run ───────────────────────────────────────────────────────────────────

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """A row scholar says it read metadata from must carry metadata.

        Scholar's whole job is turning a file into artist/title/album. If
        the probe silently returns nothing -- an unreadable container, a
        tag dialect it does not parse -- the row still advances and every
        later stage inherits a blank identity it cannot recover.
        """
        rows = ctx.conn.execute(
            """
            SELECT a.file_path, a.artist, a.title FROM archive a
              JOIN events e ON e.file_path = a.file_path
             WHERE e.run_id = ? AND e.event_type IN ('METADATA_EXTRACTED')
             ORDER BY e.id DESC LIMIT 10
            """,
            (ctx.run_id,),
        ).fetchall()
        if not rows:
            return []
        blank = [
            Path(r["file_path"]).name
            for r in rows
            if not (r["artist"] or "").strip() and not (r["title"] or "").strip()
        ]
        if not blank:
            return []
        return [
            f"{len(blank)} of {len(rows)} row(s) were read but carry neither "
            f"artist nor title: {', '.join(blank[:3])}"
        ]

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)
        pending = _get_hashed(ctx.conn)

        _COMMIT_EVERY = 50  # commit progress incrementally
        named_from_file = 0

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

            # The archive row gets the file name's artist/title when the file
            # has neither tag. metadata_cache below keeps what the tags said,
            # because it is the record of the file itself.
            named = dict(meta)
            from_file = False
            if not meta.get("artist") and not meta.get("title"):
                artist, title = _name_from_file(path)
                if artist:
                    named.update(artist=artist, title=title)
                    from_file = True
                    named_from_file += 1

            # Update archive
            archive_row = {"file_path": path_str, "status": "CATALOGUED", **named}
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
                    f"artist={named.get('artist')!r} "
                    f"title={named.get('title')!r} "
                    f"bitrate={meta.get('bitrate')}"
                    + ("  (no tags; artist and title from the file name)" if from_file else "")
                ),
            )
            result.files_changed += 1
            logger.info(
                "catalogued: %s — %s / %s",
                path.name,
                named.get("artist", "?"),
                named.get("title", "?"),
            )

            # Periodic commit so progress survives a crash
            if result.files_processed % _COMMIT_EVERY == 0:
                ctx.conn.commit()
                logger.info(
                    "[scholar] checkpoint %d / %d",
                    result.files_processed,
                    len(pending),
                )

        if named_from_file:
            result.notes.append(
                f"{named_from_file} file(s) had no tags; artist and title taken from the file name"
            )

        if result.files_errored > 0:
            result.success = False

        ctx.record_stage(result)
        return result
