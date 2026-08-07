#!/usr/bin/env python3
"""
MUSAEUS — Stage: AAC Car Masked
Mix subliminal noise under each AAC-Car track for background ambiance.

Purpose:
  Layers pink/brown/white noise underneath each track at barely-audible levels:
    Brown  : -20 dB (low-freq engine/road)
    Pink   : -24 dB (mid-range fill)
    White  : -28 dB (high-freq wind hiss)

  Philosophy: Fill psychoacoustic gaps during quiet passages, keeping road/wind
  noise from being the dominant sound in car silence.

Idempotent:
  Re-running skips already-masked files (use --force to re-mask).

Design:
  - Loads noise profiles from {runs_root}/Noise/
  - Scans AAC-Car source for files
  - For each track: blend selected noise underneath using ffmpeg amix
  - Parallel masking via ThreadPoolExecutor
"""

from __future__ import annotations

import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from ..config import get_config, AUDIO_EXTENSIONS
from ..context import RunContext, StageResult
from .base import BaseStage, StageError

logger = logging.getLogger(__name__)

# Noise profiles: name -> list of (colour, dB level)
NOISE_PROFILES = {
    "clean": [],  # No noise
    "pink": [("pink", -24.0)],
    "brown": [("brown", -20.0)],
    "white": [("white", -28.0)],
    "dual": [("brown", -20.0), ("pink", -24.0)],  # Default: Brown + Pink
}

DEFAULT_PROFILE = "dual"
DEFAULT_WORKERS = 4


class AACCarMaskedStage(BaseStage):
    """Mix subliminal noise under AAC-Car tracks."""

    NAME = "aac-car-mask"

    def __init__(self):
        """Initialize stage. Configuration loaded from context stash."""
        pass

    def validate(self, ctx: RunContext) -> None:
        """Check ffmpeg availability and noise files."""
        try:
            subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True,
                check=True,
                timeout=5,
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            raise StageError("ffmpeg not found or not working")

        noise_profile = ctx.get("aac_car_masked_noise_profile", DEFAULT_PROFILE)
        
        if noise_profile not in NOISE_PROFILES:
            raise StageError(f"Unknown noise profile: {noise_profile}")

        profile_spec = NOISE_PROFILES.get(noise_profile)

        if not profile_spec:
            # "clean" profile needs no files
            logger.info("[aac-car-mask] Using 'clean' profile (no noise)")
            return

        # Check required noise files exist
        for colour, _ in profile_spec:
            noise_file = self._get_noise_file(colour)
            if not noise_file or not noise_file.exists():
                raise StageError(
                    f"Noise file not found for {colour}: {noise_file}\n"
                    f"Run 'musaeus noise-gen' first."
                )

    def _get_noise_file(self, colour: str) -> Optional[Path]:
        """Get path to 30-minute noise file for given colour."""
        cfg = get_config()
        # Use 30-minute noise tracks (shorter, sufficient for looping)
        return cfg.noise_dir / f"{colour.capitalize()}_Noise_30min.m4a"

    def _find_source_dir(self) -> Path:
        """Resolve AAC-Car source directory."""
        cfg = get_config()

        # Priority 1: AAC-Car/BATCH_001 (standard output from AACCarStage)
        batch_001 = cfg.aac_car_root / "BATCH_001"
        if batch_001.exists():
            audio_count = sum(1 for _ in batch_001.rglob("*.m4a"))
            if audio_count > 0:
                logger.info("Using AAC-Car/BATCH_001 (%d files)", audio_count)
                return batch_001

        # Priority 2: AAC-Car root (flat structure)
        if cfg.aac_car_root.exists():
            audio_count = sum(1 for _ in cfg.aac_car_root.rglob("*.m4a"))
            if audio_count > 0:
                logger.info("Using AAC-Car root (%d files)", audio_count)
                return cfg.aac_car_root

        raise StageError(f"No AAC-Car source found at {cfg.aac_car_root}")

    def _gather_source_files(self, source_dir: Path) -> list[Path]:
        """Collect all AAC files from source directory."""
        files = []
        for path in sorted(source_dir.rglob("*.m4a")):
            if path.is_file():
                files.append(path)
        return files

    def _derive_output_path(self, source_path: Path, source_dir: Path) -> Path:
        """Map source file to output path, preserving structure."""
        cfg = get_config()
        relative = source_path.relative_to(source_dir)
        return cfg.aac_car_masked_root / relative

    def _build_ffmpeg_amix_filter(self) -> tuple[str, int]:
        """
        Build ffmpeg filter graph for noise mixing.

        Returns:
          (filter_string, num_inputs)

        Example with dual profile (Brown + Pink):
          Input 0: track
          Input 1: Brown noise (looped)
          Input 2: Pink noise (looped)

          Filter: [0][1][2]amix=inputs=3:duration=longest[out]
          Output: [out]
        """
        profile_spec = NOISE_PROFILES.get(self.noise_profile, [])

        if not profile_spec:
            # "clean" — no mixing needed
            return "", 1

        # Build list of inputs: track + noise files
        num_inputs = 1 + len(profile_spec)

        # Build filter: [0][1][2]...[n]amix=inputs=N:duration=longest
        input_pads = "".join([f"[{i}]" for i in range(num_inputs)])
        filter_str = f"{input_pads}amix=inputs={num_inputs}:duration=longest[out]"

        return filter_str, num_inputs

    def _build_ffmpeg_command(self, source_path: Path, output_path: Path) -> list[str]:
        """Build complete ffmpeg command for masking a single file."""
        profile_spec = NOISE_PROFILES.get(self.noise_profile, [])

        cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-i", str(source_path)]

        # If clean profile, just copy the file
        if not profile_spec:
            cmd.extend([
                "-c:a",
                "copy",
                "-map_metadata",
                "0",
                str(output_path),
            ])
            return cmd

        # Add noise inputs and build amix filter
        for colour, _ in profile_spec:
            noise_file = self._get_noise_file(colour)
            cmd.extend(["-i", str(noise_file)])

        filter_str, _ = self._build_ffmpeg_amix_filter()

        # Apply amix filter, then encode to AAC
        cmd.extend([
            "-filter_complex",
            filter_str,
            "-map",
            "[out]",
            "-c:a",
            "aac",
            "-b:a",
            "256k",
            "-map_metadata",
            "0",
            str(output_path),
        ])

        return cmd

    def _mask_one(self, source_path: Path, source_dir: Path) -> tuple[str, bool]:
        """Mask a single file. Returns (status_line, success)."""
        output_path = self._derive_output_path(source_path, source_dir)

        if output_path.exists():
            return f"SKIP   {source_path.name}", True

        cmd = self._build_ffmpeg_command(source_path, output_path)

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=180,
                check=True,
            )
            return f"MASK   {source_path.name} → {output_path.name}", True
        except subprocess.CalledProcessError as e:
            logger.warning(
                "Failed to mask %s: %s",
                source_path.name,
                e.stderr[-200:] if e.stderr else str(e),
            )
            return f"ERROR  {source_path.name}", False
        except subprocess.TimeoutExpired:
            logger.warning("Timeout masking %s (>180s)", source_path.name)
            return f"ERROR  {source_path.name} (timeout)", False

    def dry_run(self, ctx: RunContext) -> StageResult:
        """Preview files to mask."""
        result = self._make_result(dry_run=True)
        cfg = get_config()
        noise_profile = ctx.get("aac_car_masked_noise_profile", DEFAULT_PROFILE)

        try:
            source_dir = self._find_source_dir()
        except StageError as e:
            result.success = False
            result.errors.append(str(e))
            ctx.record_stage(result)
            return result

        files = self._gather_source_files(source_dir)

        profile_spec = NOISE_PROFILES.get(noise_profile, [])
        noise_str = " + ".join([f"{c} ({db:+.0f}dB)" for c, db in profile_spec]) or "clean"

        logger.info("[aac-car-mask] DRY RUN: Would mask %d files", len(files))
        logger.info("[aac-car-mask] Profile: %s (%s)", noise_profile, noise_str)
        logger.info("[aac-car-mask] Output: %s", cfg.aac_car_masked_root)

        for f in files[:20]:
            logger.info("  [DRY]  %s", f.name)

        if len(files) > 20:
            logger.info("  ... and %d more", len(files) - 20)

        result.details["would_mask"] = len(files)
        result.details["noise_profile"] = noise_profile
        ctx.record_stage(result)
        return result

    def run(self, ctx: RunContext) -> StageResult:
        """Mask all AAC-Car files with noise."""
        result = self._make_result(dry_run=False)
        cfg = get_config()
        noise_profile = ctx.get("aac_car_masked_noise_profile", DEFAULT_PROFILE)
        workers = ctx.get("aac_car_masked_workers", DEFAULT_WORKERS)

        try:
            source_dir = self._find_source_dir()
        except StageError as e:
            result.success = False
            result.errors.append(str(e))
            ctx.record_stage(result)
            return result

        cfg.ensure_dirs()
        files = self._gather_source_files(source_dir)

        profile_spec = NOISE_PROFILES.get(noise_profile, [])
        noise_str = " + ".join([f"{c} ({db:+.0f}dB)" for c, db in profile_spec]) or "clean"

        logger.info("[aac-car-mask] Masking %d files", len(files))
        logger.info("[aac-car-mask] Source: %s", source_dir)
        logger.info("[aac-car-mask] Output: %s", cfg.aac_car_masked_root)
        logger.info("[aac-car-mask] Profile: %s (%s)", noise_profile, noise_str)
        logger.info("[aac-car-mask] Workers: %d", workers)

        ok_count = 0
        error_count = 0

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(self._mask_one, f, source_dir)
                for f in files
            ]
            for future in as_completed(futures):
                status, ok = future.result()
                logger.info("  %s", status)
                if ok:
                    ok_count += 1
                else:
                    error_count += 1
                    result.errors.append(status)

        result.details["masked"] = ok_count
        result.details["errors"] = error_count
        result.details["total"] = len(files)
        result.details["noise_profile"] = noise_profile
        result.success = error_count == 0

        ctx.record_stage(result)
        return result
