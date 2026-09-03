#!/usr/bin/env python3
r"""
╔══════════════════════════════════════════════════════════════════╗
║  ORPHEUS — NOISE MASKER (Sebring Car Audio)                      ║
║  Version   : 1.0                                                 ║
║  Updated   : 2026-07-03                                          ║
║  Author    : Grey + Claude                                       ║
╠══════════════════════════════════════════════════════════════════╣
║  PURPOSE                                                         ║
║  Mixes Brown + Pink + White noise UNDER each AAC-Car track       ║
║  to mask wind, road, and engine noise in the Sebring             ║
║  convertible. Preserves original files — writes to a new         ║
║  AAC-Car-Masked output tree. Resume-safe: skips completed files. ║
╠══════════════════════════════════════════════════════════════════╣
║  NOISE LEVELS (Sebring convertible starting point)               ║
║  Brown  : -12 dB  — masks low-frequency engine/road rumble       ║
║  Pink   : -15 dB  — balanced mid-range fill                      ║
║  White  : -18 dB  — light high-frequency wind hiss mask          ║
╠══════════════════════════════════════════════════════════════════╣
║  USAGE                                                           ║
║  python3 orpheus_noise_masker.py              ← dry-run preview   ║
║  python3 orpheus_noise_masker.py --apply      ← process all      ║
║  python3 orpheus_noise_masker.py --apply --limit 5   ← test 5    ║
║  python3 orpheus_noise_masker.py --apply --workers 8 ← faster    ║
║  python3 orpheus_noise_masker.py --brown-db -10 --pink-db -13 \  ║
║            --white-db -16                    ← custom levels      ║
╚══════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
#
# NOISE_DIR previously hardcoded ORPHEUS_ROOT with no override -- confirmed
# 2026-08-16 as a real isolation hazard (any invocation, from any caller,
# always pulled noise source files from the live/suspended ORPHEUS install,
# with no way to redirect via env var or the --src/--out flags below, which
# only ever covered AAC_CAR_SRC/AAC_CAR_OUT). Now reads ORPHEUS_NOISE_DIR
# from the environment first; the old hardcoded path is only a fallback
# default for the case where nothing else has set it, not a forced value.
# Repointed 2026-08-21 from /mnt/FORGE2TB/ACTIVE_PROJECTS/ORPHEUS, which is
# being retired off FORGE2TB. See the note above: the override was already
# doing the real work, because the old default directory did not exist.
VAULT_ROOT = Path(os.environ.get("MUSAEUS_VAULT_ROOT", "/mnt/FORGE2TB/Projects/MUSAEUS_VAULT"))
NOISE_DIR = Path(os.environ.get("ORPHEUS_NOISE_DIR", str(VAULT_ROOT / "RUNS" / "Noise")))
AAC_CAR_SRC = VAULT_ROOT / "RUNS" / "AAC-Car"
AAC_CAR_OUT = VAULT_ROOT / "RUNS" / "AAC-Car-Masked"

# 30-min files loop cleanly for any typical track
NOISE_FILES = {
    "brown": NOISE_DIR / "Brown_Noise_30min.m4a",
    "pink": NOISE_DIR / "Pink_Noise_30min.m4a",
    "white": NOISE_DIR / "White_Noise_30min.m4a",
}

OUTPUT_BITRATE = "256k"

# Container/codec rounding shifts reported duration slightly on a correct
# encode; same value and rationale as the generator's.
_DURATION_TOLERANCE_SEC = 2.0


# ── Data ──────────────────────────────────────────────────────────────────────


@dataclass
class Job:
    src: Path
    dst: Path
    brown_db: float
    pink_db: float
    white_db: float


# ── Core processing ───────────────────────────────────────────────────────────


def get_duration(path: Path) -> float | None:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        return None


def get_sample_rate(path: Path) -> int | None:
    """Sample rate of the first audio stream, or None when unreadable."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return int(result.stdout.strip().rstrip(","))
    except (ValueError, AttributeError):
        return None


def _output_is_complete(src: Path, dst: Path) -> bool:
    """True when *dst* is a finished mask of *src*.

    Existence alone is not enough: an interrupted run leaves a short file
    that a later run would skip for ever. Duration against the source is the
    cheap check that catches it, and the rate has to match too -- an output
    written before the rate was pinned is wrong even at full length.
    """
    if not dst.is_file():
        return False
    src_dur, dst_dur = get_duration(src), get_duration(dst)
    if src_dur is None or dst_dur is None:
        return False
    if abs(src_dur - dst_dur) > _DURATION_TOLERANCE_SEC:
        return False
    return get_sample_rate(dst) == get_sample_rate(src)


def mix_track(job: Job) -> tuple[bool, str]:
    """Mix noise under one track. Returns (success, message)."""
    if _output_is_complete(job.src, job.dst):
        return True, f"SKIP (done): {job.dst.name}"

    duration = get_duration(job.src)
    if duration is None:
        return False, f"FAIL (no duration): {job.src.name}"

    # State the output rate explicitly instead of leaving it to filter-graph
    # negotiation. Measured 2026-09-01: the graph does resolve to the music's
    # rate today, including against the 96 kHz beds sitting in the live
    # library -- so this is not repairing an active defect. But nothing in the
    # command said so, and the rate the car library ships at should not depend
    # on how amix happens to negotiate between four inputs. The encoder decides
    # the rate; the masker's job is to not change it, in writing.
    src_rate = get_sample_rate(job.src)
    if src_rate is None:
        return False, f"FAIL (no sample rate): {job.src.name}"

    job.dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = job.dst.with_suffix(".tmp.m4a")

    brown = str(NOISE_FILES["brown"])
    pink = str(NOISE_FILES["pink"])
    white = str(NOISE_FILES["white"])

    # Build filter: attenuate each noise colour, blend them, mix under music
    filt = (
        f"[1:a]volume={job.brown_db}dB[b];"
        f"[2:a]volume={job.pink_db}dB[p];"
        f"[3:a]volume={job.white_db}dB[w];"
        f"[b][p][w]amix=inputs=3:normalize=0[noise];"
        f"[0:a][noise]amix=inputs=2:normalize=0:duration=first[out]"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(job.src),
        "-stream_loop",
        "-1",
        "-t",
        str(duration),
        "-i",
        brown,
        "-stream_loop",
        "-1",
        "-t",
        str(duration),
        "-i",
        pink,
        "-stream_loop",
        "-1",
        "-t",
        str(duration),
        "-i",
        white,
        "-filter_complex",
        filt,
        "-map",
        "[out]",
        "-c:a",
        "aac",
        "-b:a",
        OUTPUT_BITRATE,
        "-ar",
        str(src_rate),
        "-movflags",
        "+faststart",
        str(tmp),
    ]

    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        if tmp.exists():
            tmp.unlink()
        err = result.stderr[-200:].decode(errors="replace").strip()
        return False, f"FAIL: {job.src.name} — {err}"

    # Verify before publishing. ffmpeg can exit 0 having written a file that
    # is short or at the wrong rate; renaming it into place would make it
    # indistinguishable from a good one on the next run.
    if not _output_is_complete(job.src, tmp):
        detail = f"duration={get_duration(tmp)}, rate={get_sample_rate(tmp)}"
        tmp.unlink(missing_ok=True)
        return False, f"FAIL (verify): {job.src.name} — {detail}"

    tmp.rename(job.dst)
    return True, f"OK: {job.dst.name}"


def worker(job: Job) -> tuple[bool, str]:
    """Multiprocessing entry point."""
    try:
        return mix_track(job)
    except Exception as e:
        return False, f"ERROR: {job.src.name} — {e}"


# ── Job collection ─────────────────────────────────────────────────────────────


def collect_jobs(
    src_root: Path,
    dst_root: Path,
    brown_db: float,
    pink_db: float,
    white_db: float,
    limit: int | None,
) -> list[Job]:
    jobs = []
    files = sorted(src_root.rglob("*.m4a"))
    if limit is not None:
        files = files[:limit]
    for src in files:
        rel = src.relative_to(src_root)
        dst = dst_root / rel
        jobs.append(Job(src=src, dst=dst, brown_db=brown_db, pink_db=pink_db, white_db=white_db))
    return jobs


# ── Progress helper ────────────────────────────────────────────────────────────


def fmt_eta(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ORPHEUS Noise Masker — mix noise under AAC-Car tracks"
    )
    parser.add_argument("--apply", action="store_true", help="Actually process files")
    parser.add_argument("--limit", type=int, default=None, help="Cap number of tracks")
    parser.add_argument(
        "--workers", type=int, default=4, help="Parallel ffmpeg workers (default: 4)"
    )
    parser.add_argument(
        "--brown-db",
        type=float,
        default=-12.0,
        help="Brown noise level in dB (default: -12)",
    )
    parser.add_argument(
        "--pink-db",
        type=float,
        default=-15.0,
        help="Pink noise level in dB (default: -15)",
    )
    parser.add_argument(
        "--white-db",
        type=float,
        default=-18.0,
        help="White noise level in dB (default: -18)",
    )
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    parser.add_argument("--src", default=str(AAC_CAR_SRC), help="Source AAC-Car root")
    parser.add_argument("--out", default=str(AAC_CAR_OUT), help="Output root")
    args = parser.parse_args()

    src_root = Path(args.src)
    dst_root = Path(args.out)

    print("═" * 60)
    print("   ORPHEUS — NOISE MASKER v1.0")
    print("═" * 60)
    print(f"   Source   : {src_root}")
    print(f"   Output   : {dst_root}")
    print(
        f"   Noise    : Brown {args.brown_db:+.0f}dB | Pink {args.pink_db:+.0f}dB | White {args.white_db:+.0f}dB"
    )
    print(f"   Workers  : {args.workers}")
    print(f"   Mode     : {'APPLY' if args.apply else 'DRY RUN'}")
    print()

    # Validate noise files
    for colour, path in NOISE_FILES.items():
        if not path.exists():
            print(f"   ERROR: {colour} noise file not found: {path}")
            sys.exit(1)

    if not src_root.exists():
        print(f"   ERROR: Source not found: {src_root}")
        sys.exit(1)

    print("   Scanning tracks...")
    jobs = collect_jobs(src_root, dst_root, args.brown_db, args.pink_db, args.white_db, args.limit)

    already_done = sum(1 for j in jobs if j.dst.exists())
    to_process = len(jobs) - already_done

    print(f"   Total    : {len(jobs):,} tracks")
    print(f"   Done     : {already_done:,} (will skip)")
    print(f"   To do    : {to_process:,}")
    print()

    if not args.apply:
        print("   DRY RUN — pass --apply to process files.")
        print("   Sample output paths:")
        for j in jobs[:3]:
            print(f"     {j.src.name}")
            print(f"     → {j.dst.relative_to(dst_root)}")
        print("═" * 60)
        return

    if to_process == 0:
        print("   Nothing to do — all tracks already processed.")
        print("═" * 60)
        return

    if not args.yes:
        confirm = (
            input(f"   Process {to_process:,} tracks with {args.workers} workers? [y/N] ")
            .strip()
            .lower()
        )
        if confirm != "y":
            print("   Cancelled.")
            return

    print()

    start = time.time()
    done = 0
    skipped = 0
    failed = 0
    failed_list: list[str] = []

    with multiprocessing.Pool(processes=args.workers) as pool:
        for success, msg in pool.imap_unordered(worker, jobs):
            if "SKIP" in msg:
                skipped += 1
            elif success:
                done += 1
            else:
                failed += 1
                failed_list.append(msg)

            total_processed = done + skipped + failed
            elapsed = time.time() - start
            rate = done / elapsed if elapsed > 0 and done > 0 else 0
            remaining = (to_process - done) / rate if rate > 0 else 0

            print(
                f"  [{total_processed:>5}/{len(jobs):>5}] "
                f"done={done} skip={skipped} fail={failed} "
                f"| {fmt_eta(remaining)} remaining"
                f"  {msg[:60]}",
                flush=True,
            )

    elapsed = time.time() - start
    print()
    print("═" * 60)
    print(f"   Done     : {done:,}")
    print(f"   Skipped  : {skipped:,}")
    print(f"   Failed   : {failed:,}")
    print(f"   Elapsed  : {fmt_eta(elapsed)}")
    print("═" * 60)

    if failed_list:
        print(f"\n   Failed tracks ({len(failed_list)}):")
        for msg in failed_list[:20]:
            print(f"     {msg}")
        if len(failed_list) > 20:
            print(f"     ... and {len(failed_list) - 20} more")

    if failed == 0:
        print("   STATUS: ✓ ALL TRACKS MASKED")
    else:
        print(f"   STATUS: ⚠ COMPLETE WITH {failed} FAILURES")
    print("═" * 60)


if __name__ == "__main__":
    main()
