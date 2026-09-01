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
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from musaeus.config import get_config  # noqa: E402
from musaeus.db import open_db  # noqa: E402
from musaeus.hasher import audio_hash_safe  # noqa: E402
from musaeus.idle_throttle import IdleThrottle  # noqa: E402

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


def stage_from_catalogue(
    conn, staging_dir: Path, limit: int | None = None,
    edition: str = "car", budget_bytes: int | None = None,
) -> tuple[list[Path], list]:
    """Symlink every CATALOGUED master into *staging_dir* for the encoder.

    The vendor encoder discovers work by walking a directory with
    rglob("*.m4a") and accepts no file list, so aiming it straight at
    ALAC_Archive is not an option: the review folders now live inside the
    archive, and DUPES_MOVED_FOR_REVIEW / TRIBUTE_REMOVED_FOR_REVIEW hold
    masters deliberately set aside. Walking the archive would encode
    removed knock-offs back into the car and quietly undo the removal.

    So the catalogue decides, not the filesystem. Selection returns only
    CATALOGUED rows, which by definition excludes anything quarantined,
    staged for dupe review, or gone.

    Symlinks rather than copies: 453 GB of masters staged by reference
    costs nothing and cannot modify the originals. rglob sees a symlink to
    a file as a file, and ffmpeg reads through it.
    """
    from musaeus.editions import EDITIONS, output_path_for, select_edition

    spec = EDITIONS[edition]
    sel = select_edition(conn, spec, budget_bytes=budget_bytes)
    if sel.skipped_for_budget:
        print(f"  {len(sel.skipped_for_budget):,} track(s) do not fit the "
              f"{budget_bytes / 1_000_000_000:.0f} GB budget; lowest-priority "
              "genres are dropped first.")
    tracks = sel.included[:limit] if limit else sel.included

    staged: list[Path] = []
    for t in tracks:
        src = Path(t.file_path)
        if not src.is_file():
            continue
        link = output_path_for(t, spec, staging_dir)
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(src)
        staged.append(link)
    return staged, tracks


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MUSAEUS Car-Library Export -- AAC encode + optional noise masking"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--mask", action="store_true", help="Apply noise masking, skip the prompt")
    group.add_argument("--no-mask", action="store_true", help="Skip masking, skip the prompt")
    parser.add_argument("--from-catalogue", action="store_true",
                        help="Build from every CATALOGUED master rather than from "
                             "files hand-dropped into the input folder")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report the plan and encode nothing")
    parser.add_argument("--limit", type=int, metavar="N", default=None,
                        help="Stage at most N tracks (for a test build)")
    parser.add_argument("--edition", choices=("car", "iphone"), default="car",
                        help="Which edition to build. iPhone is the same format "
                             "(AAC 256k, -14 LUFS, <=48 kHz) with a size budget "
                             "and no masking -- headphones have no road noise to "
                             "mask. Not a fourth script with its own spelling of "
                             "--execute; an edition differing only in selection.")
    parser.add_argument("--budget-gb", type=float, metavar="GB", default=None,
                        help="Device budget. Required in practice for iphone: "
                             "81.7 GB of library does not fit a 30 GB phone.")
    args = parser.parse_args()

    cfg = get_config()
    input_dir = cfg.runs_root / "AAC-Car-Masked"
    output_dir = input_dir / "_output"
    encoded_dir = output_dir / "encoded"
    masked_dir = output_dir / "masked"

    input_dir.mkdir(parents=True, exist_ok=True)

    if args.from_catalogue:
        # Per-process staging. A shared "_staged" is not safe: this function
        # rmtree's it on entry and again after a dry run, so a preview run
        # deletes the symlink tree a LIVE encode is reading from. That
        # happened 2026-09-01 -- an iPhone dry run pulled the staging out
        # from under a running Car build at file 4,860, and the encoder
        # spent the next 3,713 files reporting "No such file or directory".
        # The encode was unharmed (already-converted output is skipped on a
        # re-run) but hours of wall clock were lost to a preview.
        staging_dir = input_dir / f"_staged_{os.getpid()}"
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)
        conn_sel = open_db(cfg.db_path)
        try:
            files, tracks = stage_from_catalogue(
                conn_sel, staging_dir, args.limit,
                edition=args.edition,
                budget_bytes=int(args.budget_gb * 1_000_000_000) if args.budget_gb else None,
            )
        finally:
            conn_sel.close()
        input_dir = staging_dir
        est_gb = sum(
            t.duration * 256 * 1000 / 8 * 1.02 for t in tracks
        ) / 1_000_000_000
        print(f"Staged {len(files):,} master(s) from the catalogue "
              f"(~{est_gb:.1f} GB of AAC to write).")
        if args.dry_run:
            print("\n[DRY RUN] Nothing encoded. First 10 of the plan:")
            for t in tracks[:10]:
                print(f"    [{t.genre or '-'}] {t.artist} — {t.title}")
            print(f"\n    ... {len(tracks):,} track(s) total.")
            shutil.rmtree(staging_dir, ignore_errors=True)
            return 0
    else:
        files = find_input_files(input_dir)
    if not files:
        print(f"No audio files found in {input_dir}")
        print("Place the ALAC/FLAC files you want converted there and re-run.")
        return 0

    print(f"Found {len(files)} file(s) in {input_dir}:")
    for f in files:
        print(f"  {f.name}")

    if args.edition == "iphone" and not args.mask:
        # Masking exists to sit under road noise in a car. On headphones it
        # is just noise mixed into the music.
        apply_masking = False
        print("iPhone edition: masking off (no cabin noise to mask).")
    elif args.mask:
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
    # An encode runs for hours and only writes to the DB at the very end.
    # Losing that write to a lock means the files are on disk but unrecorded,
    # and the next run re-encodes all of them. Measured 2026-08-31: a 5-track
    # build died with "database is locked" because an acousticid drain held
    # the write lock -- the audio was fine, the bookkeeping was gone. Wait for
    # the lock rather than throwing away the work.
    cfg_db.execute("PRAGMA busy_timeout = 300000")  # 5 minutes
    source_rows: dict[Path, dict | None] = {}
    if args.from_catalogue:
        # The staged entries are symlinks to masters we selected FROM the
        # database, so the row is already known. Re-deriving it by hashing
        # would decode all 10,545 files to rediscover what selection just
        # told us -- hours of CPU to answer a question we already answered.
        for link in files:
            target = str(Path(link).resolve())
            row = cfg_db.execute(
                "SELECT file_path, artist, title FROM archive WHERE file_path = ?",
                (target,),
            ).fetchone()
            source_rows[Path(link)] = dict(row) if row else None
            if row is None:
                print(f"  WARNING: staged {Path(link).name} resolves to {target}, "
                      "which has no archive row")
    else:
        # Hand-dropped files: identity has to be rediscovered from the audio
        # itself, because nothing says where they came from.
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
                print(f"  WARNING: {src.name} has no matching archive row "
                      "(not a known MUSAEUS file)")

    # Stage 1: encode
    encoded_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["ORPHEUS_AAC_INPUT_DIR"] = str(input_dir)
    env["ORPHEUS_AAC_OUTPUT_DIR"] = str(encoded_dir)
    # Also needed by the encode step's copy_noise_tracks(), not just by the
    # masker below -- without it the vendor falls back to ORPHEUS's RUNS.
    env["ORPHEUS_NOISE_DIR"] = str(cfg.runs_root / "Noise")
    print("\n=== Encoding (ALAC/FLAC -> 256k AAC) ===")
    # A full edition is ~44 h of ffmpeg and drives load past 9 on 8 cores.
    # Always on: it costs nothing when the machine is idle, and it is the
    # difference between a build you can leave running and one you cannot
    # use the machine through. MUSAEUS_NO_IDLE_THROTTLE=1 opts out.
    with IdleThrottle():
        result = subprocess.run(
            [sys.executable, str(VENDOR_DIR / "build_aac_library.py"),
             "--profile", "car", "--apply"],
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
        with IdleThrottle():
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
