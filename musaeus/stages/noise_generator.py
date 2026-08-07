#!/usr/bin/env python3
"""
MUSAEUS — Stage: NoiseGenerator
Generate subliminal noise tracks (Pink/Brown/White) for the AAC-Car-Masked pipeline.

Purpose:
  Creates 30-minute and 60-minute noise samples at -16 LUFS using ffmpeg lavfi.
  Generates: Pink_Noise_30min.m4a, Brown_Noise_30min.m4a, White_Noise_30min.m4a
             Pink_Noise_60min.m4a, Brown_Noise_60min.m4a, White_Noise_60min.m4a

Idempotent:
  Re-running skips files that already exist (use --force to regenerate).

Design:
  - Pass 1: Generate raw noise to temp FLAC file (ffmpeg lavfi anoisesrc)
  - Pass 2: Measure integrated loudness (ffmpeg loudnorm json output)
  - Pass 3: Encode to AAC m4a with linear normalization (loudnorm + aac)
  - Cleanup temp files
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..config import get_config
from ..context import RunContext, StageResult
from .base import BaseStage, StageError

logger = logging.getLogger(__name__)

# Noise spec: (colour, duration_min, track_num)
NOISE_TRACKS = [
    ("pink", 30, 1),
    ("brown", 30, 2),
    ("white", 30, 3),
    ("pink", 60, 4),
    ("brown", 60, 5),
    ("white", 60, 6),
]

# Loudness targets (match ORPHEUS)
TARGET_LUFS = -16.0
TARGET_TP = -1.0
TARGET_LRA = 11.0

# Encoding
AAC_BITRATE = "256k"
SAMPLE_RATE = 44100


class NoiseGeneratorStage(BaseStage):
    """Generate subliminal noise samples for AAC-Car masking."""

    NAME = "noise-gen"

    def validate(self, ctx: RunContext) -> None:
        """Check that ffmpeg and ffprobe are available."""
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

    def _output_path(self, colour: str, duration_min: int) -> Path:
        """Path to output noise file."""
        cfg = get_config()
        return cfg.noise_dir / f"{colour.capitalize()}_Noise_{duration_min}min.m4a"

    def _title(self, colour: str, duration_min: int) -> str:
        """Metadata title for the noise track."""
        return f"{colour.capitalize()} Noise {duration_min}min"

    def _generate_raw(self, colour: str, duration_sec: int, tmp_path: Path, dry_run: bool) -> bool:
        """Pass 1: Generate raw noise to lossless temp file."""
        if dry_run:
            return True

        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostats",
            "-f",
            "lavfi",
            "-i",
            f"anoisesrc=colour={colour}:duration={duration_sec}:sample_rate={SAMPLE_RATE}",
            "-c:a",
            "flac",
            str(tmp_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=True)
            return True
        except subprocess.CalledProcessError as e:
            logger.error("Failed to generate raw %s noise: %s", colour, e.stderr[-400:] if e.stderr else str(e))
            return False
        except subprocess.TimeoutExpired:
            logger.error("Timeout generating raw %s noise (>600s)", colour)
            return False

    def _measure(self, tmp_path: Path, dry_run: bool) -> dict | None:
        """Pass 2: Measure integrated loudness."""
        if dry_run:
            return {
                "input_i": str(TARGET_LUFS),
                "input_tp": str(TARGET_TP),
                "input_lra": str(TARGET_LRA),
                "input_thresh": "-70.0",
                "target_offset": "0.0",
            }

        filter_arg = (
            f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TP}:LRA={TARGET_LRA}:print_format=json"
        )
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(tmp_path),
            "-af",
            filter_arg,
            "-f",
            "null",
            "-",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=True)
            # Extract JSON from stderr
            stderr = result.stderr
            start = stderr.rfind("{")
            end = stderr.rfind("}")
            if start == -1 or end == -1:
                logger.error("No loudnorm JSON found in ffmpeg output for %s", tmp_path.name)
                return None
            stats = json.loads(stderr[start : end + 1])
            return stats
        except subprocess.CalledProcessError as e:
            logger.error("Failed to measure loudness: %s", e.stderr[-400:] if e.stderr else str(e))
            return None
        except subprocess.TimeoutExpired:
            logger.error("Timeout measuring loudness (>300s)")
            return None
        except json.JSONDecodeError as e:
            logger.error("Failed to parse loudnorm JSON: %s", e)
            return None

    def _encode(
        self,
        tmp_path: Path,
        out_path: Path,
        stats: dict,
        colour: str,
        duration_min: int,
        track_num: int,
        dry_run: bool,
    ) -> bool:
        """Pass 3: Encode to AAC m4a with linear normalization."""
        if dry_run:
            return True

        filter_arg = (
            f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TP}:LRA={TARGET_LRA}:"
            f"measured_I={stats['input_i']}:"
            f"measured_TP={stats['input_tp']}:"
            f"measured_LRA={stats['input_lra']}:"
            f"measured_thresh={stats['input_thresh']}:"
            f"offset={stats['target_offset']}:"
            "linear=true:print_format=summary"
        )

        title = self._title(colour, duration_min)
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostats",
            "-i",
            str(tmp_path),
            "-af",
            filter_arg,
            "-c:a",
            "aac",
            "-b:a",
            AAC_BITRATE,
            "-vn",
            "-sn",
            "-metadata",
            f"title={title}",
            "-metadata",
            "artist=MUSAEUS",
            "-metadata",
            "album_artist=MUSAEUS",
            "-metadata",
            "album=Acoustic Treatment",
            "-metadata",
            "genre=Noise",
            "-metadata",
            "date=2026",
            "-metadata",
            f"track={track_num}",
            str(out_path),
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=True)
            return True
        except subprocess.CalledProcessError as e:
            logger.error("Failed to encode %s: %s", out_path.name, e.stderr[-400:] if e.stderr else str(e))
            return False
        except subprocess.TimeoutExpired:
            logger.error("Timeout encoding %s (>600s)", out_path.name)
            return False

    def _generate_one(self, colour: str, duration_min: int, track_num: int, force: bool, dry_run: bool) -> tuple[str, bool]:
        """Generate a single noise track. Returns (status_line, success)."""
        cfg = get_config()
        out_path = self._output_path(colour, duration_min)

        if out_path.exists() and not force:
            return f"  SKIP   {out_path.name} (exists)", True

        duration_sec = duration_min * 60
        tmp_path = cfg.noise_dir / f"_tmp_{colour}_{duration_min}min.flac"

        try:
            if dry_run:
                return f"  [DRY]  {out_path.name} ({duration_min}min, {TARGET_LUFS} LUFS)", True

            # Pass 1: Generate raw noise
            if not self._generate_raw(colour, duration_sec, tmp_path, dry_run=False):
                return f"  ERROR  {out_path.name} (generate raw failed)", False

            # Pass 2: Measure loudness
            stats = self._measure(tmp_path, dry_run=False)
            if stats is None:
                return f"  ERROR  {out_path.name} (measure failed)", False

            # Pass 3: Encode to AAC
            out_path.parent.mkdir(parents=True, exist_ok=True)
            if not self._encode(tmp_path, out_path, stats, colour, duration_min, track_num, dry_run=False):
                return f"  ERROR  {out_path.name} (encode failed)", False

            return f"  OK     {out_path.name} ({duration_min}min, {TARGET_LUFS} LUFS)", True

        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception as e:
                    logger.warning("Failed to delete temp file %s: %s", tmp_path, e)

    def dry_run(self, ctx: RunContext) -> StageResult:
        """Preview what would be generated."""
        result = self._make_result(dry_run=True)
        cfg = get_config()

        logger.info("[noise-gen] DRY RUN - Would generate %d noise tracks", len(NOISE_TRACKS))
        logger.info("[noise-gen] Output: %s", cfg.noise_dir)

        for colour, duration_min, track_num in NOISE_TRACKS:
            status, ok = self._generate_one(colour, duration_min, track_num, force=False, dry_run=True)
            logger.info(status)
            if not ok:
                result.errors.append(status)

        result.details["would_generate"] = len(NOISE_TRACKS)
        ctx.record_stage(result)
        return result

    def run(self, ctx: RunContext) -> StageResult:
        """Generate all noise tracks."""
        result = self._make_result(dry_run=False)
        cfg = get_config()

        logger.info("[noise-gen] Generating %d noise tracks", len(NOISE_TRACKS))
        logger.info("[noise-gen] Output: %s", cfg.noise_dir)
        logger.info("[noise-gen] Target: %s LUFS / %s AAC", TARGET_LUFS, AAC_BITRATE)

        cfg.ensure_dirs()

        ok_count = 0
        for colour, duration_min, track_num in NOISE_TRACKS:
            status, ok = self._generate_one(colour, duration_min, track_num, force=False, dry_run=False)
            logger.info(status)
            if ok:
                ok_count += 1
            else:
                result.errors.append(status)

        result.details["generated"] = ok_count
        result.details["total"] = len(NOISE_TRACKS)
        result.success = ok_count == len(NOISE_TRACKS)

        ctx.record_stage(result)
        return result
