#!/usr/bin/env python3
r"""
MUSAEUS — Stage: Transcode
Convert lossless archive files (FLAC/ALAC/WAV) to 256k AAC for portable export.

What it does:
  - Finds CATALOGUED lossless files that haven't been transcoded yet
  - Uses ffmpeg to encode each to 256k AAC (.m4a) in the transcode output dir
  - Output path: <transcode_root>/<Artist>/<Album>/<track> - <title>.m4a
  - Copies embedded album art from the source file
  - Writes basic tags (artist, album, title, year, track, genre) to the output
  - Stores transcode_path + transcode_at in archive (auto-migrated columns)
  - Logs TRANSCODE_DONE event per file
  - dry_run() reports what would be transcoded without doing any work
  - Re-run safe: already-transcoded files are skipped unless --force

Design:
  - Lossless only — determined by real ffprobe codec (archive.codec, via
    config.LOSSLESS_CODECS), never by file extension. Fixes a real bug:
    the old extension-based LOSSLESS_EXTENSIONS check excludes .m4a
    entirely, which misses every ALAC-in-.m4a file in a real MUSAEUS
    library (canonicalize.py already made this same fix; see its module
    docstring for the full incident).
  - AAC 256k via libfdk_aac if available, falls back to aac encoder
  - Embedded album art is detected via canonicalize.py's
    _has_attached_picture()/_probe_streams() (reused, not reimplemented)
    and explicitly re-mapped through with -c:v copy when present. Fixes a
    second real bug: the ffmpeg command previously had an unconditional
    -vn ("no video stream"), silently stripping art from every file
    regardless of source content -- directly contradicting this
    docstring's own "copies embedded album art" claim above. Confirmed via
    the live archive.db that this bug never actually reached a real file:
    transcode_path/transcode_at didn't exist as columns yet (auto-created
    on first real run), zero TRANSCODE_DONE events, empty Transcoded/
    output dir -- the stage had simply never been run for real, in part
    because the .m4a-extension-filter bug above meant it had no eligible
    real rows to process either. Both bugs fixed together in the same
    change since the second was silently masking the first.
  - Output filenames are Windows-safe (strips :*?"<>|\ chars)
  - Periodic DB commits every _COMMIT_EVERY files

ORPHEUS equivalent: SCRIPTS/build_aac_library.py
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from ..config import LOSSLESS_CODECS as _LOSSLESS_CODECS
from ..context import RunContext, StageResult
from ..db import ensure_columns
from .base import BaseStage, StageError
from .canonicalize import _has_attached_picture, _probe_streams

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 25
_FFMPEG_TIMEOUT = 300  # 5 min per file max (large FLACs)
_TARGET_BITRATE = "256k"
_CONTAINER = "m4a"

# Windows-safe filename chars
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|]')


# ── Helpers ───────────────────────────────────────────────────────────────────


def _safe_name(s: str, max_len: int = 60) -> str:
    """Strip unsafe chars and truncate for filesystem safety."""
    s = _UNSAFE_CHARS.sub("_", s).strip(". ")
    return s[:max_len] if len(s) > max_len else s


def _ensure_columns(conn) -> None:  # type: ignore[type-arg]
    """Columns this stage owns. Mechanism shared via db.ensure_columns;
    the list stays here, next to the code that reads them."""
    ensure_columns(
        conn,
        (
            ("transcode_path", "TEXT"),
            ("transcode_at", "TEXT"),
        ),
    )
def _best_aac_encoder() -> str:
    """Return libfdk_aac if ffmpeg has it, else built-in aac."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return "aac"
    try:
        res = subprocess.run([ffmpeg, "-encoders"], capture_output=True, text=True, timeout=10)
        if "libfdk_aac" in res.stdout:
            return "libfdk_aac"
    except Exception:
        pass
    return "aac"


def _transcode_file(
    src: Path,
    dst: Path,
    encoder: str,
    artist: str,
    album: str,
    title: str,
    year: str,
    track: str,
    genre: str,
) -> None:
    """
    Transcode src → dst using ffmpeg.
    Raises ValueError on failure or timeout.

    Art handling mirrors canonicalize.py's _convert_to_alac()/
    _transcode_to_aac() exactly: probe for an attached_pic video stream
    first, map + copy it through when present instead of unconditionally
    dropping video via -vn.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found in PATH")

    dst.parent.mkdir(parents=True, exist_ok=True)

    try:
        has_art = _has_attached_picture(_probe_streams(src))
    except Exception:
        # Probe failure shouldn't block the transcode -- fall back to
        # audio-only, same as if the source genuinely had no art.
        has_art = False

    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-nostats",
        "-i",
        str(src),
    ]
    if has_art:
        cmd += ["-map", "0:a:0", "-map", "0:v:0"]
    else:
        cmd += ["-map", "0:a:0"]
    cmd += [
        "-c:a",
        encoder,
        "-b:a",
        _TARGET_BITRATE,
    ]
    if has_art:
        cmd += ["-c:v", "copy", "-disposition:v:0", "attached_pic"]
    cmd += [
        "-movflags",
        "+faststart",  # web-friendly MP4 header
        # Tags
        "-metadata",
        f"artist={artist}",
        "-metadata",
        f"album={album}",
        "-metadata",
        f"title={title}",
        "-metadata",
        f"date={year}",
        "-metadata",
        f"track={track}",
        "-metadata",
        f"genre={genre}",
        str(dst),
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"ffmpeg timed out after {_FFMPEG_TIMEOUT}s for {src.name}") from exc

    if res.returncode != 0:
        raise ValueError(f"ffmpeg failed (rc={res.returncode}): {res.stderr[-300:]}")


# ── Stage ─────────────────────────────────────────────────────────────────────


class TranscodeStage(BaseStage):
    """
    Transcode — lossless → 256k AAC for portable device export.
    """

    NAME = "transcode"

    def validate(self, ctx: RunContext) -> None:
        if not shutil.which("ffmpeg"):
            raise StageError("ffmpeg not found in PATH — Transcode requires ffmpeg.")

        transcode_root = ctx.get("transcode_root") or (ctx.config.vault_root / "Transcoded")
        logger.info("[transcode] output root: %s", transcode_root)

        try:
            count = ctx.conn.execute(
                "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' AND transcode_path IS NULL"
            ).fetchone()[0]
        except Exception:
            count = ctx.conn.execute(
                "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'"
            ).fetchone()[0]
        logger.info("[transcode] %d lossless file(s) to transcode", count)

    def _transcode(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)

        if not dry_run:
            _ensure_columns(ctx.conn)

        transcode_root = Path(ctx.get("transcode_root") or (ctx.config.vault_root / "Transcoded"))
        force = ctx.get("transcode_force", False)
        encoder = _best_aac_encoder()
        logger.info("[transcode] using encoder: %s", encoder)

        # Only transcode lossless sources
        try:
            where_extra = "" if force else "AND transcode_path IS NULL"
            rows = ctx.conn.execute(
                f"""
                SELECT file_path, artist, album, title, year, track, genre, codec
                FROM archive
                WHERE status = 'CATALOGUED'
                  {where_extra}
                ORDER BY artist, album, track
                """
            ).fetchall()
        except Exception:
            rows = ctx.conn.execute(
                """
                SELECT file_path, artist, album, title, year, track, genre, codec
                FROM archive
                WHERE status = 'CATALOGUED'
                ORDER BY artist, album, track
                """
            ).fetchall()

        # Filter to lossless only by real ffprobe codec (archive.codec),
        # never by file extension -- .m4a can hold either ALAC (lossless)
        # or AAC (lossy), so an extension-only check misses every
        # ALAC-in-.m4a file. Matches canonicalize.py's own codec check.
        rows = [r for r in rows if (r["codec"] or "").lower() in _LOSSLESS_CODECS]

        transcoded = 0
        skipped = 0
        errors = 0

        from datetime import datetime, timezone

        now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

        for row in rows:
            result.files_processed += 1
            src = Path(row["file_path"])

            if not src.exists():
                result.files_skipped += 1
                skipped += 1
                continue

            artist = _safe_name(row["artist"] or "Unknown Artist")
            album = _safe_name(row["album"] or "Unknown Album")
            title = _safe_name(row["title"] or src.stem)
            year = str(row["year"] or "")
            track = str(row["track"] or "")
            genre = str(row["genre"] or "")

            track_prefix = f"{int(track):02d} - " if track.isdigit() else ""
            dst_name = f"{track_prefix}{title}.{_CONTAINER}"
            dst = transcode_root / artist / album / dst_name

            if not force and dst.exists():
                result.files_skipped += 1
                skipped += 1
                continue

            logger.info("[transcode] %s  →  %s", src.name, dst.relative_to(transcode_root))

            if not dry_run:
                try:
                    _transcode_file(
                        src,
                        dst,
                        encoder,
                        row["artist"] or "",
                        row["album"] or "",
                        row["title"] or src.stem,
                        year,
                        track,
                        genre,
                    )
                    ctx.conn.execute(
                        "UPDATE archive SET transcode_path=?, transcode_at=? WHERE file_path=?",
                        (str(dst), now, row["file_path"]),
                    )
                    ctx.log_event(
                        "TRANSCODE_DONE",
                        file_path=row["file_path"],
                        new_value=str(dst),
                        stage=self.NAME,
                        note=f"encoder={encoder}",
                    )
                    transcoded += 1
                    result.files_changed += 1
                except Exception as exc:
                    logger.warning("[transcode] error %s: %s", src.name, exc)
                    errors += 1
                    result.files_skipped += 1
                    result.errors.append(f"{src.name}: {exc}")
            else:
                transcoded += 1
                result.files_changed += 1

            if result.files_processed % _COMMIT_EVERY == 0 and not dry_run:
                ctx.conn.commit()
                logger.info("[transcode] checkpoint %d", result.files_processed)

        prefix = "Would transcode" if dry_run else "Transcoded"
        result.notes.append(
            f"{prefix} {transcoded} lossless file(s) → 256k AAC. "
            f"Skipped {skipped}, errors {errors}."
        )
        if transcoded and not dry_run:
            result.notes.append(f"Output: {transcode_root}")

        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._transcode(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._transcode(ctx, dry_run=False)
