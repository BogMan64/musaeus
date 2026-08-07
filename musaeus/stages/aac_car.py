#!/usr/bin/env python3
"""
MUSAEUS — Stage: AAC Car
Encode ALAC/FLAC source → AAC-Car 256k with EBU R128 loudness normalization.

Purpose:
  Two-pass LUFS normalization:
    Pass 1: Measure integrated loudness of source file
    Pass 2: Encode to AAC with linear gain applied

Idempotent:
  Re-running skips already-encoded files (use --force to re-encode).

Design:
  - Scans source directory for audio files
  - For each file: measure LUFS → encode with normalization applied
  - Preserves metadata (artist, album, title, genre, year, etc.)
  - Maintains directory structure: Artist / Album / Track.m4a
  - Parallel encoding via ThreadPoolExecutor
"""

from __future__ import annotations

import json
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from ..config import get_config, AUDIO_EXTENSIONS, LOSSLESS_EXTENSIONS
from ..context import RunContext, StageResult
from .base import BaseStage, StageError

logger = logging.getLogger(__name__)

# Encoding spec
AAC_BITRATE = "256k"
TARGET_LUFS = -14.0  # Car profile: slightly louder than portable (-16.0)
TARGET_TP = -1.0
TARGET_LRA = 11.0
SAMPLE_RATE = 44100

# Parallel workers
DEFAULT_WORKERS = 4


class AACCarStage(BaseStage):
    """Encode ALAC/FLAC source → AAC-Car 256k."""

    NAME = "aac-car"

    def __init__(self):
        """Initialize stage. Configuration loaded from context stash."""
        pass

    def validate(self, ctx: RunContext) -> None:
        """Check ffmpeg/ffprobe availability and source directory."""
        try:
            subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True,
                check=True,
                timeout=5,
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            raise StageError("ffmpeg not found or not working")

        try:
            subprocess.run(
                ["ffprobe", "-version"],
                capture_output=True,
                check=True,
                timeout=5,
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            raise StageError("ffprobe not found or not working")

    def _find_source_dir(self) -> Path:
        """Resolve source directory priority: alac_source_dir → first FORGED in archive."""
        cfg = get_config()

        # Priority 1: Explicit ALAC source directory
        if cfg.alac_source_dir and cfg.alac_source_dir.exists():
            audio_count = sum(1 for _ in cfg.alac_source_dir.rglob(f"*[{','.join(LOSSLESS_EXTENSIONS)}]"))
            if audio_count > 0:
                logger.info("Using explicit ALAC source: %s (%d files)", cfg.alac_source_dir, audio_count)
                return cfg.alac_source_dir

        # Priority 2: Vault RUNS/ALAC_SOURCE fallback
        alac_source = cfg.runs_root / "ALAC_SOURCE"
        if alac_source.exists():
            audio_count = sum(1 for _ in alac_source.rglob(f"*[{','.join(LOSSLESS_EXTENSIONS)}]"))
            if audio_count > 0:
                logger.info("Using ALAC_SOURCE: %s (%d files)", alac_source, audio_count)
                return alac_source

        raise StageError(
            f"No ALAC source found. Set MUSAEUS_ALAC_SOURCE_DIR or populate {alac_source}"
        )

    def _gather_source_files(self, source_dir: Path) -> list[Path]:
        """Collect all lossless audio files from source directory."""
        files = []
        for path in sorted(source_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in LOSSLESS_EXTENSIONS:
                files.append(path)
        return files

    def _derive_output_path(self, source_path: Path, tags: dict) -> Path:
        """Derive Artist/Album/Track.m4a path from metadata."""
        cfg = get_config()
        output_root = cfg.aac_car_root / "BATCH_001"

        # Extract metadata
        artist = tags.get("artist", "Unknown Artist").strip()
        album = tags.get("album", "Unknown Album").strip()
        title = tags.get("title", source_path.stem).strip()

        # Sanitise path components
        artist_safe = self._sanitise(artist)
        album_safe = self._sanitise(album)

        output_file = output_root / artist_safe / album_safe / f"{title}.m4a"
        return output_file

    @staticmethod
    def _sanitise(name: str) -> str:
        """Sanitise string for filesystem path (remove/replace invalid chars)."""
        import re

        # Remove leading/trailing whitespace
        name = name.strip()
        # Replace problematic chars with underscore
        name = re.sub(r'[/\\:*?"<>|]', "_", name)
        # Collapse multiple underscores
        name = re.sub(r"_+", "_", name).strip("_")
        return name or "Unknown"

    def _ffprobe_metadata(self, file_path: Path) -> dict[str, str]:
        """Extract metadata from audio file using ffprobe."""
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(file_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
            data = json.loads(result.stdout)

            format_tags = {str(k).lower(): str(v) for k, v in (data.get("format", {}).get("tags", {}) or {}).items()}
            audio_tags = {}

            for stream in data.get("streams", []) or []:
                if stream.get("codec_type") == "audio":
                    stream_tags = {str(k).lower(): str(v) for k, v in (stream.get("tags", {}) or {}).items()}
                    audio_tags.update(stream_tags)

            merged = {}
            merged.update(format_tags)
            merged.update(audio_tags)
            return merged
        except Exception as e:
            logger.warning("Failed to extract metadata from %s: %s", file_path.name, e)
            return {"title": file_path.stem}

    def _measure(self, source_path: Path) -> Optional[dict]:
        """Pass 1: Measure integrated loudness."""
        filter_arg = f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TP}:LRA={TARGET_LRA}:print_format=json"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(source_path),
            "-af",
            filter_arg,
            "-f",
            "null",
            "-",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=True)
            stderr = result.stderr
            start = stderr.rfind("{")
            end = stderr.rfind("}")
            if start == -1 or end == -1:
                logger.warning("No loudnorm JSON in ffmpeg output for %s", source_path.name)
                return None
            return json.loads(stderr[start : end + 1])
        except subprocess.CalledProcessError as e:
            logger.warning("Failed to measure %s: %s", source_path.name, e.stderr[-200:] if e.stderr else str(e))
            return None
        except subprocess.TimeoutExpired:
            logger.warning("Timeout measuring %s (>120s)", source_path.name)
            return None
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse loudnorm JSON for %s: %s", source_path.name, e)
            return None

    def _encode(
        self,
        source_path: Path,
        output_path: Path,
        tags: dict,
        stats: dict,
    ) -> bool:
        """Pass 2: Encode to AAC with linear normalization applied."""
        filter_arg = (
            f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TP}:LRA={TARGET_LRA}:"
            f"measured_I={stats['input_i']}:"
            f"measured_TP={stats['input_tp']}:"
            f"measured_LRA={stats['input_lra']}:"
            f"measured_thresh={stats['input_thresh']}:"
            f"offset={stats['target_offset']}:"
            "linear=true:print_format=summary"
        )

        # Build metadata arguments
        meta_args = []
        for key in ["artist", "album", "album_artist", "title", "genre", "year", "track"]:
            if key in tags:
                meta_args.extend(["-metadata", f"{key}={tags[key]}"])

        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostats",
            "-i",
            str(source_path),
            "-af",
            filter_arg,
            "-c:a",
            "aac",
            "-b:a",
            AAC_BITRATE,
            "-map_metadata",
            "0",
            *meta_args,
        ]

        # Include attached picture if present
        has_picture = self._has_attached_picture(source_path)
        if has_picture:
            cmd.extend(["-map", "0:v:0", "-c:v", "copy", "-disposition:v:0", "attached_pic"])
        else:
            cmd.append("-vn")

        cmd.append(str(output_path))

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=True)
            return True
        except subprocess.CalledProcessError as e:
            logger.warning("Failed to encode %s: %s", source_path.name, e.stderr[-200:] if e.stderr else str(e))
            return False
        except subprocess.TimeoutExpired:
            logger.warning("Timeout encoding %s (>180s)", source_path.name)
            return False

    def _has_attached_picture(self, file_path: Path) -> bool:
        """Check if file has an attached picture stream."""
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            str(file_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=True)
            data = json.loads(result.stdout)
            for stream in data.get("streams", []) or []:
                if stream.get("codec_type") == "video":
                    disp = stream.get("disposition", {}) or {}
                    if disp.get("attached_pic") == 1:
                        return True
            return False
        except Exception:
            return False

    def _encode_one(self, source_path: Path) -> tuple[str, bool]:
        """Encode a single file. Returns (status_line, success)."""
        output_path = self._derive_output_path(source_path, self._ffprobe_metadata(source_path))

        if output_path.exists():
            return f"SKIP   {source_path.name} → {output_path.name}", True

        # Pass 1: Measure
        stats = self._measure(source_path)
        if stats is None:
            return f"ERROR  {source_path.name} (measure failed)", False

        # Pass 2: Encode
        tags = self._ffprobe_metadata(source_path)
        if not self._encode(source_path, output_path, tags, stats):
            return f"ERROR  {source_path.name} (encode failed)", False

        return f"ENCODE {source_path.name} → {output_path.name}", True

    def dry_run(self, ctx: RunContext) -> StageResult:
        """Preview files to encode."""
        result = self._make_result(dry_run=True)

        try:
            source_dir = self._find_source_dir()
        except StageError as e:
            result.success = False
            result.errors.append(str(e))
            ctx.record_stage(result)
            return result

        files = self._gather_source_files(source_dir)
        logger.info("[aac-car] DRY RUN: Would encode %d files", len(files))
        logger.info("[aac-car] Target: %s LUFS / %s AAC", TARGET_LUFS, AAC_BITRATE)

        for f in files[:20]:
            logger.info("  [DRY]  %s", f.name)

        if len(files) > 20:
            logger.info("  ... and %d more", len(files) - 20)

        result.details["would_encode"] = len(files)
        ctx.record_stage(result)
        return result

    def run(self, ctx: RunContext) -> StageResult:
        """Encode all source files to AAC-Car."""
        result = self._make_result(dry_run=False)
        cfg = get_config()
        workers = ctx.get("aac_car_workers", DEFAULT_WORKERS)

        try:
            source_dir = self._find_source_dir()
        except StageError as e:
            result.success = False
            result.errors.append(str(e))
            ctx.record_stage(result)
            return result

        cfg.ensure_dirs()
        files = self._gather_source_files(source_dir)

        logger.info("[aac-car] Encoding %d files", len(files))
        logger.info("[aac-car] Source: %s", source_dir)
        logger.info("[aac-car] Output: %s", cfg.aac_car_root)
        logger.info("[aac-car] Target: %s LUFS / %s AAC / %d workers", TARGET_LUFS, AAC_BITRATE, workers)

        ok_count = 0
        error_count = 0

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(self._encode_one, f) for f in files]
            for future in as_completed(futures):
                status, ok = future.result()
                logger.info("  %s", status)
                if ok:
                    ok_count += 1
                else:
                    error_count += 1
                    result.errors.append(status)

        result.details["encoded"] = ok_count
        result.details["errors"] = error_count
        result.details["total"] = len(files)
        result.success = error_count == 0

        ctx.record_stage(result)
        return result
