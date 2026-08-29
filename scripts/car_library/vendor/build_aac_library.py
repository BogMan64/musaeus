#!/usr/bin/env python3
"""
ORPHEUS — Build AAC Library
Console: [18] Build AAC Car / [19] Build AAC Port
Purpose: Parallel two-pass LUFS normalization and AAC encode for car or portable profiles from the ALAC archive.
Usage:   python3 build_aac_library.py --profile car|port [--apply] [--workers N]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil as _shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from lib.orpheus_naming import (
    AUDIO_EXTENSIONS,
    build_track_filename,
    clean_album_for_path,
    clean_metadata_from_tags,
    ffmpeg_metadata_args,
    log_metadata_cleaning_event,
    log_naming_event,
    resolve_album_artist_for_path,
)
from lib.orpheus_paths import (
    AAC_CAR_EXPORT,
    AAC_PORT_EXPORT,
    ALAC_BATCH_001,
    INBOX_CURRENT,
    LUFS_AAC_CAR_NORMALIZED,
    LUFS_AAC_PORTABLE_NORMALIZED,
    LUFS_ARCHIVE_NORMALIZED,
    MUSIC_VAULT_ALAC,
    RUNS_ROOT,
)
from source_quality_policy import should_make_aac

INPUT_DIR = ALAC_BATCH_001
FALLBACK_SOURCE = INBOX_CURRENT

# Primary ALAC source (already populated by [17] Build ALAC)
_ALAC_VAULT = MUSIC_VAULT_ALAC

# Forge normalized output locations (preferred source when present)
_FORGE_ARCHIVE = LUFS_ARCHIVE_NORMALIZED
_FORGE_CAR = LUFS_AAC_CAR_NORMALIZED
_FORGE_PORTABLE = LUFS_AAC_PORTABLE_NORMALIZED

# Profile → preferred Forge source (falls back to INPUT_DIR if not present / empty)
_FORGE_SOURCES: dict[str, Path] = {
    "car": _FORGE_CAR,
    "portable": _FORGE_PORTABLE,
}


def resolve_input_dir(profile_name: str) -> Path:
    """
    Returns the best available input directory for a given profile.

    Priority:
      1. Forge-normalized output for this profile  (RUNS/LUFS_NORMALIZED/…)
      2. ALAC Vault — Music.Vault/ALAC/             (populated by [17] Build ALAC)
      3. Original ALAC batch folder                 (EXPORTS/ALAC_LIBRARY/BATCH_001)
      4. Conversion inbox fallback                  (CONVERSION_INBOX/CURRENT)

    Prints which source was selected so the operator always knows.

    Priority 0 (added 2026-08-16, for the standalone MUSAEUS Car-Library
    wrapper): if ORPHEUS_AAC_INPUT_DIR is set, use it unconditionally,
    ahead of the whole ORPHEUS-internal fallback chain below -- this
    script otherwise has no CLI arg for the input source at all, which
    is wrong for a caller that isn't ORPHEUS itself.
    """
    env_override = os.environ.get("ORPHEUS_AAC_INPUT_DIR")
    if env_override:
        override_path = Path(env_override)
        audio_count = sum(1 for _ in override_path.rglob("*.m4a")) if override_path.exists() else 0
        print(f"[Override] Using ORPHEUS_AAC_INPUT_DIR ({audio_count} files): {override_path}")
        return override_path

    # Priority 1: Forge-normalized output for this profile
    forge_src = _FORGE_SOURCES.get(profile_name)
    if forge_src and forge_src.exists():
        audio_count = sum(1 for _ in forge_src.rglob("*.m4a"))
        if audio_count > 0:
            print(
                f"[Forge] Using LUFS-normalized source ({audio_count} files): {forge_src}"
            )
            return forge_src

    # Priority 2: ALAC Vault (primary, already populated)
    if _ALAC_VAULT.exists():
        audio_count = sum(1 for _ in _ALAC_VAULT.rglob("*.m4a"))
        if audio_count > 0:
            print(f"[Vault] Using ALAC vault ({audio_count} files): {_ALAC_VAULT}")
            return _ALAC_VAULT

    # Priority 3: Original ALAC batch folder
    if INPUT_DIR.exists():
        audio_count = sum(1 for _ in INPUT_DIR.rglob("*.m4a"))
        if audio_count > 0:
            print(
                f"[Source] Using ALAC batch folder ({audio_count} files): {INPUT_DIR}"
            )
            return INPUT_DIR

    # Priority 4: Conversion inbox fallback (rebuild from scratch)
    if FALLBACK_SOURCE.exists():
        audio_count = sum(1 for _ in FALLBACK_SOURCE.rglob("*"))
        if audio_count > 0:
            print(
                f"[Fallback] Using conversion inbox ({audio_count} files): {FALLBACK_SOURCE}"
            )
            return FALLBACK_SOURCE

    print(f"[Cache] Using original ALAC source: {INPUT_DIR}")
    return INPUT_DIR


FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

MAX_WORKERS = 4
OVERWRITE = True

DEFAULT_LUFS = -16.0

# Two-pass loudnorm targets not covered by the per-profile target_lufs value
# below. Matches ORPHEUS's own normalize_selected_lufs.py defaults -- no
# MUSAEUS-specific reason to diverge, and keeping them the same avoids a
# second, unexplained set of loudness constants in the codebase.
TARGET_TP = "-1.0"
TARGET_LRA = "11.0"

# Tolerance for post-bake duration comparison (seconds), same rationale and
# same value as canonicalize.py's _DURATION_TOLERANCE_SEC: container/codec
# differences (and AAC encoder priming samples) can shift reported duration
# slightly even when the audio content is correct.
_DURATION_TOLERANCE_SEC = 1.5

PROFILES = {
    "car": {
        "folder": "AAC_CAR",
        "audio_bitrate": "256k",
        "target_lufs": -14.0,
        "output_root": AAC_CAR_EXPORT,
    },
    "portable": {
        "folder": "AAC_PORTABLE",
        "audio_bitrate": "192k",
        "target_lufs": DEFAULT_LUFS,
        "output_root": AAC_PORT_EXPORT,
    },
    "port": {  # alias for portable
        "folder": "AAC_PORTABLE",
        "audio_bitrate": "192k",
        "target_lufs": DEFAULT_LUFS,
        "output_root": AAC_PORT_EXPORT,
    },
}


def normalize_tag_dict(tags: dict[str, str]) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in tags.items()}


def first_nonempty(*values: str | None) -> str | None:
    for value in values:
        if value is not None:
            value = str(value).strip()
            if value:
                return value
    return None


def ffprobe_metadata(file_path: Path) -> tuple[dict[str, str], bool]:
    cmd = [
        FFPROBE,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(file_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)

    format_tags = normalize_tag_dict(data.get("format", {}).get("tags", {}) or {})
    streams = data.get("streams", []) or []

    stream_tags: dict[str, str] = {}
    has_attached_picture = False

    for stream in streams:
        codec_type = stream.get("codec_type")
        disposition = stream.get("disposition", {}) or {}
        tags = normalize_tag_dict(stream.get("tags", {}) or {})

        if codec_type == "audio":
            stream_tags.update(tags)

        if codec_type == "video" and disposition.get("attached_pic") == 1:
            has_attached_picture = True

    merged = {}
    merged.update(stream_tags)
    merged.update(format_tags)

    return merged, has_attached_picture


def derive_output_path(
    file_path: Path, tags: dict[str, str], output_root: Path, profile_folder: str
) -> tuple[Path, dict[str, str]]:
    clean_tags = clean_metadata_from_tags(tags, fallback_title=file_path.stem)

    artist_resolution = resolve_album_artist_for_path(
        clean_tags.get("album_artist", ""),
        clean_tags.get("artist", ""),
    )

    artist_safe = artist_resolution.path_artist
    album_safe = clean_album_for_path(clean_tags.get("album", "Unknown Album"))

    output_dir = output_root / "BATCH_001" / artist_safe / album_safe
    output_file = output_dir / build_track_filename(
        artist_resolution.canonical_artist,
        clean_tags.get("title", file_path.stem),
        ".m4a",
    )

    log_metadata_cleaning_event(
        source_script="build_aac_library.py",
        source_path=file_path,
        raw_tags=tags,
        clean_tags=clean_tags,
        details={"profile_folder": profile_folder},
    )

    log_naming_event(
        source_script="build_aac_library.py",
        source_path=file_path,
        destination_path=output_file,
        raw_artist=clean_tags.get("album_artist", ""),
        album=clean_tags.get("album", ""),
        title=clean_tags.get("title", ""),
        filename=output_file.name,
        method=artist_resolution.method,
        details={
            "profile_folder": profile_folder,
            "matched": artist_resolution.matched,
            "confidence": artist_resolution.confidence,
            "policy": "Album Artist / Album / Album Artist - Title.m4a",
        },
    )

    return output_file, clean_tags


# ── Two-pass loudnorm (measure + bake) ───────────────────────────────────────
#
# Ported from ORPHEUS's own normalize_selected_lufs.py (confirmed working
# two-pass EBU R128 implementation) rather than reimplemented from scratch.
# This closes the gap flagged in MUSAEUS_OPEN_ITEMS.md: PROFILES[...]["target_lufs"]
# was defined but never actually consumed anywhere in this file -- the AAC
# encode ran with no loudness normalization at all despite this module's own
# docstring claiming "two-pass LUFS normalization and AAC encode" as its
# purpose.


def ffmpeg_measure_loudnorm(path: Path, target_i: str, target_tp: str, target_lra: str) -> dict:
    """Pass 1: analysis-only loudnorm run, returns ffmpeg's measured_* JSON block."""
    filter_str = f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json"
    cmd = [FFMPEG, "-hide_banner", "-nostats", "-i", str(path), "-af", filter_str, "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    stderr = result.stderr or ""

    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError(f"Could not parse loudnorm measurement for: {path}")

    return json.loads(stderr[start : end + 1])


def build_second_pass_filter(measured: dict, target_i: str, target_tp: str, target_lra: str) -> str:
    """Pass 2 filter string: applies the actual normalization using pass-1's measured_* values."""
    return (
        f"loudnorm="
        f"I={target_i}:TP={target_tp}:LRA={target_lra}:"
        f"measured_I={measured['input_i']}:"
        f"measured_LRA={measured['input_lra']}:"
        f"measured_TP={measured['input_tp']}:"
        f"measured_thresh={measured['input_thresh']}:"
        f"offset={measured['target_offset']}:"
        f"linear=true:print_format=summary"
    )


def _verify_bake(source: Path, output: Path) -> None:
    """
    Post-bake check, same discipline as canonicalize.py's _verify_conversion:
    output must have an audio stream, and duration must match the source
    within tolerance. Raises RuntimeError on any mismatch -- caller must not
    trust the output.
    """

    def _probe(p: Path) -> dict:
        cmd = [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"ffprobe failed on {p} ({proc.returncode}): {proc.stderr[:200]}")
        return json.loads(proc.stdout)

    src_probe = _probe(source)
    out_probe = _probe(output)

    out_audio = [s for s in out_probe.get("streams", []) if s.get("codec_type") == "audio"]
    if not out_audio:
        raise RuntimeError("verification failed: baked output has no audio stream")

    def _duration(probe: dict) -> float | None:
        d = probe.get("format", {}).get("duration")
        try:
            return float(d) if d else None
        except (TypeError, ValueError):
            return None

    src_dur = _duration(src_probe)
    out_dur = _duration(out_probe)
    if src_dur is not None and out_dur is not None and abs(src_dur - out_dur) > _DURATION_TOLERANCE_SEC:
        raise RuntimeError(
            f"verification failed: duration mismatch (source={src_dur:.2f}s, output={out_dur:.2f}s)"
        )


def _quarantine_failed_tmp(tmp_output: Path | None) -> None:
    """
    Leave a failed bake attempt visible instead of silently deleting it --
    same rationale as canonicalize.py's _quarantine_failed_staging: a
    FAILED_VERIFY file left in the output tree is itself a signal something
    needs manual review, and never being silently retried or dropped means
    no partial/unverified bake can ever be mistaken for a good one.
    """
    if tmp_output is None or not tmp_output.exists():
        return
    failed_path = tmp_output.with_name(tmp_output.name + ".FAILED_VERIFY")
    with contextlib.suppress(OSError):
        tmp_output.rename(failed_path)


def build_ffmpeg_command(
    input_file: Path,
    output_file: Path,
    bitrate: str,
    has_attached_picture: bool,
    clean_tags: dict[str, str],
    loudnorm_filter: str,
) -> list[str]:
    cmd = [FFMPEG]

    cmd.append("-y" if OVERWRITE else "-n")
    cmd += ["-i", str(input_file)]

    metadata_args = ffmpeg_metadata_args(clean_tags)

    # -f mp4 forced explicitly (not inferred from output_file's extension):
    # the caller writes to a .bake_tmp-suffixed temp path first (verify-then-
    # atomic-swap, matching canonicalize.py), so the on-disk extension at
    # write time is not reliably ".m4a".
    if has_attached_picture:
        cmd += [
            "-map",
            "0:a:0",
            "-map",
            "0:v:0",
            "-c:a",
            "aac",
            "-b:a",
            bitrate,
            "-af",
            loudnorm_filter,
            "-c:v",
            "copy",
            "-disposition:v:0",
            "attached_pic",
            "-map_metadata",
            "0",
            *metadata_args,
            "-f",
            "mp4",
            str(output_file),
        ]
    else:
        cmd += [
            "-map",
            "0:a:0",
            "-c:a",
            "aac",
            "-b:a",
            bitrate,
            "-af",
            loudnorm_filter,
            "-map_metadata",
            "0",
            *metadata_args,
            "-f",
            "mp4",
            str(output_file),
        ]

    return cmd


def convert_one(file_path: Path, profile_name: str) -> str:
    allowed, policy = should_make_aac(file_path)
    if not allowed:
        return (
            f"SKIP AAC | {file_path} | "
            f"classification={policy['classification']} | "
            f"preserve_lossless={policy['preserve_lossless']} | "
            f"aac_allowed={policy['aac_allowed']}"
        )

    profile = PROFILES[profile_name]
    target_i = str(profile["target_lufs"])
    output_file: Path | None = None
    tmp_output: Path | None = None
    try:
        # Must check the same ORPHEUS_AAC_OUTPUT_DIR override main() checks --
        # this function re-derives output_root independently (it's called via
        # ThreadPoolExecutor.submit(convert_one, file_path, profile_name), so
        # it can't just receive main()'s local variable) and previously fell
        # straight back to the hardcoded profile default, silently writing
        # real output to ORPHEUS's own install path even when main() printed
        # the overridden path. Confirmed live 2026-08-16 testing this wrapper.
        env_output_override = os.environ.get("ORPHEUS_AAC_OUTPUT_DIR")
        output_root = Path(env_output_override) if env_output_override else profile["output_root"]
        tags, has_attached_picture = ffprobe_metadata(file_path)
        output_file, clean_tags = derive_output_path(
            file_path, tags, output_root, profile["folder"]
        )
        output_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_output = output_file.with_name(output_file.name + ".bake_tmp")

        measured = ffmpeg_measure_loudnorm(file_path, target_i, TARGET_TP, TARGET_LRA)
        loudnorm_filter = build_second_pass_filter(measured, target_i, TARGET_TP, TARGET_LRA)

        cmd = build_ffmpeg_command(
            input_file=file_path,
            output_file=tmp_output,
            bitrate=profile["audio_bitrate"],
            has_attached_picture=has_attached_picture,
            clean_tags=clean_tags,
            loudnorm_filter=loudnorm_filter,
        )

        subprocess.run(cmd, capture_output=True, text=True, check=True)
        _verify_bake(file_path, tmp_output)

        tmp_output.rename(output_file)
        return f"CONVERTED | {file_path.name} -> {output_file} | LUFS target={target_i}"

    except subprocess.CalledProcessError as e:
        _quarantine_failed_tmp(tmp_output)
        stderr = (e.stderr or "").strip().splitlines()
        err_line = stderr[-1] if stderr else str(e)
        return f"ERROR | {file_path.name} | {err_line}"

    except RuntimeError as e:
        _quarantine_failed_tmp(tmp_output)
        return f"ERROR | {file_path.name} | {e}"

    except Exception as e:
        _quarantine_failed_tmp(tmp_output)
        return f"ERROR | {file_path.name} | {e}"


def gather_input_files(input_dir: Path) -> list[Path]:
    files = []
    for path in sorted(input_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
            files.append(path)
    return files


def copy_noise_tracks(output_root: Path) -> None:
    """Copy generated noise tracks into the car library Artist/Album structure."""
    noise_src = RUNS_ROOT / "Noise"
    noise_dest = output_root / "ORPHEUS" / "Acoustic Treatment"

    noise_files = sorted(noise_src.glob("*.m4a")) if noise_src.exists() else []
    if not noise_files:
        print(
            "[Noise] No noise tracks found in RUNS/Noise/ — run [NO] Noise Generator first."
        )
        return

    noise_dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in noise_files:
        dst = noise_dest / src.name
        _shutil.copy2(src, dst)
        print(f"[Noise] {src.name}  →  {dst.relative_to(output_root)}")
        copied += 1
    print(f"[Noise] {copied} file(s) copied to {noise_dest}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build ORPHEUS AAC library from ALAC/FLAC source."
    )
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILES.keys()),
        required=True,
        help="AAC export profile to build.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview files to convert without encoding.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually encode files (skip dry-run prompt).",
    )
    args = parser.parse_args()
    if not args.apply:
        print("\n>>> DRY-RUN: NO changes will be written.")
        try:
            response = input("Apply changes? [y/N] ").strip().lower()
        except EOFError:
            response = ""
        if response == "y":
            args.apply = True
        else:
            print("  (dry-run confirmed — skipping writes)\n")
            return

    env_output_override = os.environ.get("ORPHEUS_AAC_OUTPUT_DIR")
    if env_output_override:
        output_root = Path(env_output_override)
    else:
        output_root = PROFILES[args.profile]["output_root"] / "BATCH_001"
    report_file = output_root / "CONVERSION_REPORT.txt"
    output_root.mkdir(parents=True, exist_ok=True)

    effective_input = resolve_input_dir(args.profile)
    files = gather_input_files(effective_input)
    if not files:
        print(f"No supported audio files found in: {effective_input}")
        print(
            "Tip: run [17] Build ALAC first to populate EXPORTS/ALAC_LIBRARY/BATCH_001"
        )
        return

    print(f"Profile: {args.profile}")
    print(f"Input:   {effective_input}")
    print(f"Output:  {output_root}")
    print(f"Files:   {len(files)}")
    print(f"Workers: {MAX_WORKERS}")
    if args.dry_run:
        print("\n  *** DRY RUN — no files will be converted ***")
        for f in sorted(files)[:20]:
            print(f"    {f.name}")
        if len(files) > 20:
            print(f"    ... and {len(files) - 20} more")
        return
    print()

    results: list[str] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(convert_one, file_path, args.profile) for file_path in files
        ]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(result)

    results.sort()
    converted = sum(1 for x in results if x.startswith("CONVERTED |"))
    errors = sum(1 for x in results if x.startswith("ERROR |"))

    summary = [
        "ORPHEUS AAC CONVERSION REPORT",
        f"Profile: {args.profile}",
        f"Input: {effective_input}",
        f"Output: {output_root}",
        f"Files found: {len(files)}",
        f"Converted: {converted}",
        f"Errors: {errors}",
        "",
        *results,
        "",
    ]
    report_file.write_text("\n".join(summary), encoding="utf-8")

    print()
    print("Done.")
    print(f"Converted: {converted}")
    print(f"Errors: {errors}")
    print(f"Report: {report_file}")

    if args.profile in {"car"}:
        print()
        copy_noise_tracks(output_root)


if __name__ == "__main__":
    main()
