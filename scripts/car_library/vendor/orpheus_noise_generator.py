#!/usr/bin/env python3
"""
ORPHEUS — Noise Generator
Console: [NO] Noise Generator
Purpose: Generates 30- and 60-minute pink, brown, and white noise AAC tracks for the car library using ffmpeg two-pass loudnorm; dry-run by default.
Usage:   python3 orpheus_noise_generator.py [--apply]

Vendored into MUSAEUS's own repo (2026-08-19), matching orpheus_noise_masker.py's
existing precedent -- rather than read from the live/suspended ORPHEUS install at
runtime. Its only ORPHEUS-internal dependency was `from lib.orpheus_paths import
RUNS_ROOT`, used solely to compute NOISE_DIR; no DB writes, no naming/metadata
event logging (unlike build_aac_library.py, which needed real ORPHEUS_ROOT/
ORPHEUS_DB_PATH overrides for exactly that reason). Patched here to read
ORPHEUS_NOISE_DIR from the environment first, same pattern orpheus_noise_masker.py
already uses -- MUSAEUS's own scripts/car_library/generate_noise.py wrapper sets
it to <vault_root>/RUNS/Noise, matching where build_car_library.py's masking step
and curator.py's _find_noise_files() already expect noise tracks to live. Falls
back to the original ORPHEUS-install path only if nothing else has set it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Repointed 2026-08-21 from /mnt/FORGE2TB/ACTIVE_PROJECTS/ORPHEUS, which is
# being retired off FORGE2TB. That constant was already vestigial: the noise
# files have lived under MUSAEUS_VAULT/RUNS/Noise for some time, and the old
# default directory did not exist at all -- so only the ORPHEUS_NOISE_DIR
# override was keeping this working. Named VAULT_ROOT now because that is
# what it actually is, and env-overridable so the next move needs no code
# change.
VAULT_ROOT = Path(os.environ.get("MUSAEUS_VAULT_ROOT", "/mnt/FORGE2TB/Projects/MUSAEUS_VAULT"))
NOISE_DIR = Path(os.environ.get("ORPHEUS_NOISE_DIR", str(VAULT_ROOT / "RUNS" / "Noise")))

TARGET_LUFS = -16.0
TARGET_TP = -1.0
TARGET_LRA = 11.0
BITRATE = "256k"
SAMPLE_RATE = 44100

# (colour, duration_min, track_num)
TRACKS = [
    ("pink", 30, 1),
    ("brown", 30, 2),
    ("white", 30, 3),
    ("pink", 60, 4),
    ("brown", 60, 5),
    ("white", 60, 6),
]


def _stem(colour: str, duration_min: int) -> str:
    return f"{colour.capitalize()}_Noise_{duration_min}min"


def _output_path(colour: str, duration_min: int) -> Path:
    return NOISE_DIR / f"{_stem(colour, duration_min)}.m4a"


def _title(colour: str, duration_min: int) -> str:
    return f"{colour.capitalize()} Noise {duration_min}min"


# ── ffmpeg helpers ─────────────────────────────────────────────────────────────


def _ffmpeg() -> str:
    ff = shutil.which("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg not found in PATH.")
    return ff


def _generate_raw(colour: str, duration_sec: int, tmp: Path) -> bool:
    """Write raw noise to a lossless temp file."""
    cmd = [
        _ffmpeg(),
        "-y",
        "-hide_banner",
        "-nostats",
        "-f",
        "lavfi",
        "-i",
        f"anoisesrc=colour={colour}:duration={duration_sec}:sample_rate={SAMPLE_RATE}",
        "-c:a",
        "flac",
        str(tmp),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(
            f"  ERROR generating raw {colour} noise:\n{result.stderr[-400:]}",
            file=sys.stderr,
        )
    return result.returncode == 0


def _measure(tmp: Path) -> dict:
    """Pass 1: measure integrated loudness of temp file."""
    filter_arg = f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TP}:LRA={TARGET_LRA}:print_format=json"
    cmd = [
        _ffmpeg(),
        "-hide_banner",
        "-nostats",
        "-i",
        str(tmp),
        "-af",
        filter_arg,
        "-f",
        "null",
        "-",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    start = res.stderr.rfind("{")
    end = res.stderr.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No loudnorm JSON in ffmpeg output for {tmp.name}")
    return json.loads(res.stderr[start : end + 1])


def _encode(
    tmp: Path, out: Path, stats: dict, colour: str, duration_min: int, track_num: int
) -> bool:
    """Pass 2: apply linear normalization and encode to AAC m4a with tags."""
    filter_arg = (
        f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TP}:LRA={TARGET_LRA}:"
        f"measured_I={stats['input_i']}:"
        f"measured_TP={stats['input_tp']}:"
        f"measured_LRA={stats['input_lra']}:"
        f"measured_thresh={stats['input_thresh']}:"
        f"offset={stats['target_offset']}:"
        "linear=true:print_format=summary"
    )
    cmd = [
        _ffmpeg(),
        "-y",
        "-hide_banner",
        "-nostats",
        "-i",
        str(tmp),
        "-af",
        filter_arg,
        "-c:a",
        "aac",
        "-b:a",
        BITRATE,
        "-vn",
        "-sn",
        "-metadata",
        f"title={_title(colour, duration_min)}",
        "-metadata",
        "artist=ORPHEUS",
        "-metadata",
        "album_artist=ORPHEUS",
        "-metadata",
        "album=Acoustic Treatment",
        "-metadata",
        "genre=Noise",
        "-metadata",
        "date=2026",
        "-metadata",
        f"track={track_num}",
        str(out),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR encoding {out.name}:\n{result.stderr[-400:]}", file=sys.stderr)
    return result.returncode == 0


# ── Per-track generation ───────────────────────────────────────────────────────


def generate_track(colour: str, duration_min: int, track_num: int, overwrite: bool) -> bool:
    out = _output_path(colour, duration_min)
    if out.exists() and not overwrite:
        print(f"  SKIP   {out.name}  (already exists — use --overwrite to replace)")
        return True

    duration_sec = duration_min * 60
    tmp = NOISE_DIR / f"_tmp_{colour}_{duration_min}min.flac"

    print(f"  GEN    {out.name}  ({duration_min}min, {TARGET_LUFS}LUFS)", flush=True)

    try:
        print(f"         step 1/3  generating {colour} noise ...", flush=True)
        if not _generate_raw(colour, duration_sec, tmp):
            return False

        print("         step 2/3  measuring loudness ...", flush=True)
        stats = _measure(tmp)

        print(f"         step 3/3  encoding AAC {BITRATE} ...", flush=True)
        ok = _encode(tmp, out, stats, colour, duration_min, track_num)
        if ok:
            dur_h = (
                f"{duration_min // 60}h{duration_min % 60:02d}m"
                if duration_min >= 60
                else f"{duration_min}min"
            )
            print(f"  OK     {out.name}  ({dur_h}, {TARGET_LUFS} LUFS)")
        return ok
    finally:
        if tmp.exists():
            tmp.unlink()


# ── Main ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="ORPHEUS Noise Generator")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Generate files (default: dry-run)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Regenerate files that already exist",
    )
    parser.add_argument(
        "--30-only",
        dest="thirty_only",
        action="store_true",
        help="Generate only 30-minute tracks (skip 60-minute)",
    )
    args = parser.parse_args()
    if not args.apply:
        print("\n>>> DRY-RUN: NO changes will be written.")
        response = input("Apply changes? [y/N] ").strip().lower()
        if response == "y":
            args.apply = True
        else:
            print("  (dry-run confirmed — skipping writes)\n")

    tracks = [(c, d, n) for c, d, n in TRACKS if not (args.thirty_only and d == 60)]

    print("\nORPHEUS Noise Generator")
    print(f"  Output : {NOISE_DIR}")
    print(f"  Tracks : {len(tracks)}")
    print(f"  Target : {TARGET_LUFS} LUFS / {BITRATE} AAC\n")

    if not args.apply:
        for colour, duration_min, _ in tracks:
            out = _output_path(colour, duration_min)
            exists = "EXISTS" if out.exists() else "NEW"
            print(f"  [DRY]  {out.name}  ({duration_min}min)  [{exists}]")
        print("\n  Pass --apply to generate files.")
        return

    NOISE_DIR.mkdir(parents=True, exist_ok=True)

    ok_count = 0
    for colour, duration_min, track_num in tracks:
        if generate_track(colour, duration_min, track_num, args.overwrite):
            ok_count += 1

    print(f"\n  ✓ {ok_count}/{len(tracks)} tracks generated  →  {NOISE_DIR}")


if __name__ == "__main__":
    main()
