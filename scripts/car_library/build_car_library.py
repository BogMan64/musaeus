#!/usr/bin/env python3
"""
MUSAEUS — Car-Library Export (standalone tool, NOT a pipeline stage)

Encodes ALAC/FLAC files into 256k AAC for car listening, with an
optional subliminal noise-masking pass (mixes low-level Brown/Pink/White
noise under the track to mask road/wind/engine noise).

Deliberately independent of `musaeus run` and the nightly cron — this is
not imported by musaeus/stages/__init__.py and is never invoked by the
canonical pipeline. Expected real-world usage: manually invoked, once or
twice a year, against a small hand-picked batch, not the whole library.

Workflow:
  1. Place the ALAC/FLAC files you want converted into:
       <vault_root>/RUNS/AAC-Car-Masked/
  2. Run this script. It asks whether to apply noise masking (or pass
     --mask / --no-mask to skip the prompt).
  3. Encoded (and optionally masked) output lands in a subfolder of the
     same input directory:
       <vault_root>/RUNS/AAC-Car-Masked/_output/encoded/
       <vault_root>/RUNS/AAC-Car-Masked/_output/masked/   (if masked)
  4. Each successfully processed file is matched back to its real
     musaeus.db archive row (by audio_hash of the ORIGINAL input file,
     then by (artist, title) tags to find the matching OUTPUT file --
     encoding/masking rename files based on tags, not the source
     filename, so filename-stem matching would not be reliable) and
     archive.car_export_path / archive.noise_profile are updated --
     the same two columns curator.py writes, using the same UPDATE
     statement, scoped only to the files this run actually touched
     (never the whole library, unlike curator.py's own unscoped
     `WHERE status='CATALOGUED'` query).
  5. Run `musaeus playlist` afterward to regenerate playlists including
     this export (playlist.py already prefers car_export_path when set).

Design history (2026-08-16): does NOT call ORPHEUS's own
build_aac_car_masked.py orchestrator or orpheus_car_playlist_builder.py
-- both were traced and execution-tested this session and found to have
real, reproducible defects (an unsupported --workers arg that makes
Stage 1 fail unconditionally through the orchestrator; a hardcoded
BATCH_001 assumption in the playlist stage that silently produces zero
playlists while reporting success). This wrapper instead calls the
underlying encode script (vendor/build_aac_library.py) and masking
script (vendor/orpheus_noise_masker.py) directly -- both individually
execution-verified working -- and reuses MUSAEUS's own playlist.py
(already integrated with car_export_path/noise_profile) rather than
ORPHEUS's playlist logic.

Both vendored scripts were patched (2026-08-16) to accept their
input/output/noise-source paths via environment variables
(ORPHEUS_AAC_INPUT_DIR, ORPHEUS_AAC_OUTPUT_DIR, ORPHEUS_NOISE_DIR) --
neither originally had any way to be pointed away from ORPHEUS's own
hardcoded install paths, which would otherwise have made this wrapper
either read/write the wrong (and possibly damaged/suspended) ORPHEUS
install by default.

Usage:
    python3 scripts/car_library/build_car_library.py
    python3 scripts/car_library/build_car_library.py --mask
    python3 scripts/car_library/build_car_library.py --no-mask
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from musaeus.config import get_config  # noqa: E402
from musaeus.db import open_db  # noqa: E402
from musaeus.hasher import audio_hash_safe  # noqa: E402

VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
AUDIO_EXTENSIONS = {".m4a", ".flac", ".alac", ".wav", ".aiff"}


def find_input_files(input_dir: Path) -> list[Path]:
    files = []
    for p in sorted(input_dir.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        if "_output" in p.relative_to(input_dir).parts:
            continue
        files.append(p)
    return files


def _read_tags(path: Path) -> tuple[str | None, str | None]:
    """Return (artist, title) via ffprobe, case/whitespace-normalized."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(out.stdout)
        tags = {k.lower(): v for k, v in (data.get("format", {}).get("tags", {}) or {}).items()}
        artist = (tags.get("artist") or "").strip().lower()
        title = (tags.get("title") or "").strip().lower()
        return artist or None, title or None
    except Exception:
        return None, None


def _find_output_by_tags(output_dir: Path, artist: str, title: str) -> Path | None:
    for p in output_dir.rglob("*"):
        if not p.is_file():
            continue
        a, t = _read_tags(p)
        if a == artist and t == title:
            return p
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MUSAEUS Car-Library Export -- AAC encode + optional noise masking"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--mask", action="store_true", help="Apply noise masking, skip the prompt")
    group.add_argument("--no-mask", action="store_true", help="Skip masking, skip the prompt")
    args = parser.parse_args()

    cfg = get_config()
    input_dir = cfg.runs_root / "AAC-Car-Masked"
    output_dir = input_dir / "_output"
    encoded_dir = output_dir / "encoded"
    masked_dir = output_dir / "masked"

    input_dir.mkdir(parents=True, exist_ok=True)

    files = find_input_files(input_dir)
    if not files:
        print(f"No audio files found in {input_dir}")
        print("Place the ALAC/FLAC files you want converted there and re-run.")
        return 0

    print(f"Found {len(files)} file(s) in {input_dir}:")
    for f in files:
        print(f"  {f.name}")

    if args.mask:
        apply_masking = True
    elif args.no_mask:
        apply_masking = False
    else:
        resp = input("\nApply noise masking for car listening? [y/N] ").strip().lower()
        apply_masking = resp == "y"

    # Look up each input file's real archive row up front (by audio_hash of
    # the ORIGINAL, pre-encode file -- this is a MUSAEUS-managed hash, so it
    # only matches files that are already real CATALOGUED library content,
    # not arbitrary drops).
    cfg_db = open_db(cfg.db_path)
    source_rows: dict[Path, dict | None] = {}
    for src in files:
        digest, err = audio_hash_safe(src)
        if err or not digest:
            print(f"  WARNING: could not hash {src.name}: {err}")
            source_rows[src] = None
            continue
        row = cfg_db.execute(
            "SELECT file_path, artist, title FROM archive WHERE audio_hash = ?", (digest,)
        ).fetchone()
        source_rows[src] = dict(row) if row else None
        if row is None:
            print(f"  WARNING: {src.name} has no matching archive row (not a known MUSAEUS file)")

    # Stage 1: encode
    encoded_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["ORPHEUS_AAC_INPUT_DIR"] = str(input_dir)
    env["ORPHEUS_AAC_OUTPUT_DIR"] = str(encoded_dir)
    print("\n=== Encoding (ALAC/FLAC -> 256k AAC) ===")
    result = subprocess.run(
        [sys.executable, str(VENDOR_DIR / "build_aac_library.py"), "--profile", "car", "--apply"],
        cwd=str(VENDOR_DIR),
        env=env,
    )
    if result.returncode != 0:
        print("Encode step failed -- aborting.", file=sys.stderr)
        cfg_db.close()
        return 1

    final_dir = encoded_dir
    noise_profile = "clean"

    if apply_masking:
        masked_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["ORPHEUS_NOISE_DIR"] = str(cfg.runs_root / "Noise")
        print("\n=== Masking (mixing car-cabin noise under tracks) ===")
        result = subprocess.run(
            [
                sys.executable, str(VENDOR_DIR / "orpheus_noise_masker.py"),
                "--apply", "--src", str(encoded_dir), "--out", str(masked_dir), "--yes",
            ],
            cwd=str(VENDOR_DIR),
            env=env,
        )
        if result.returncode != 0:
            print("Masking step failed -- output stops at the unmasked encode.", file=sys.stderr)
        else:
            final_dir = masked_dir
            noise_profile = "dual"

    # Match each processed file back to musaeus.db and record
    # car_export_path/noise_profile -- same mechanism curator.py uses
    # (UPDATE archive SET car_export_path=?, noise_profile=? WHERE
    # file_path=?), scoped only to files this run actually touched.
    updated = 0
    unmatched: list[tuple[Path, str]] = []
    for src in files:
        row = source_rows.get(src)
        if row is None:
            unmatched.append((src, "no matching archive row"))
            continue
        artist = (row["artist"] or "").strip().lower()
        title = (row["title"] or "").strip().lower()
        if not artist or not title:
            unmatched.append((src, "archive row missing artist/title tags to match against"))
            continue
        out_path = _find_output_by_tags(final_dir, artist, title)
        if out_path is None:
            unmatched.append((src, f"no output file found under {final_dir} matching artist/title tags"))
            continue
        cfg_db.execute(
            "UPDATE archive SET car_export_path = ?, noise_profile = ? WHERE file_path = ?",
            (str(out_path), noise_profile, row["file_path"]),
        )
        updated += 1
    cfg_db.commit()
    cfg_db.close()

    print(f"\n{updated}/{len(files)} file(s) matched to musaeus.db and recorded.")
    if unmatched:
        print(f"{len(unmatched)} file(s) could not be matched:")
        for src, reason in unmatched:
            print(f"  {src.name}: {reason}")

    print(f"\nOutput: {final_dir}")
    print("Run `musaeus playlist` to regenerate playlists including this export.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
