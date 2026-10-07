#!/usr/bin/env python3
"""Merge album folders that differ only by capitalisation.

THE PROBLEM

"Back in Black" and "Back In Black" are one album. On a folder-browsed
library they are two, and in the car they are two entries in the list with
some of the tracks in each. Measured 2026-09-16: 28 such pairs in
ALAC-Archival, 28 in ALAC_Library, 24 in CAR_Library.

ext4 is case-SENSITIVE, which is why both exist at once; a case-insensitive
filesystem would have merged them by accident long ago.

THE RULINGS THIS APPLIES, both Grey's, 2026-09-16

  which spelling wins    musaeus.title_case.album_title_case -- "at", not
                         "At". One ruling, one place, so the next caller does
                         not invent a third spelling.

  which FILE wins        when both folders hold a track of the same name:
                         best quality first, then length, longer wins.

Quality is bitrate where ffprobe will say, because that is what "quality"
means for the AAC edition; for two lossless files it is usually a tie and the
length rule decides. A tie on both keeps the file already in the winning
folder, so the operation is stable if it is run twice.

THE DATABASE MOVES WITH THE FILES

archive.file_path and archive.car_export_path are absolute. A rename that
does not carry them turns every moved row into a phantom -- the catalogue
naming a file that is not there, which is the failure the CAR rename of
earlier today had to repair two of.

    python3 scripts/merge_case_duplicate_albums.py              # dry run
    python3 scripts/merge_case_duplicate_albums.py --execute
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from musaeus.config import MusicConfig  # noqa: E402
from musaeus.title_case import album_title_case  # noqa: E402

TIERS = ("ALAC-Archival", "ALAC_Library", "CAR_Library")


def probe(p: Path) -> tuple[int, float]:
    """(bitrate, duration). Zeros when ffprobe cannot say -- an unreadable
    file must never beat a readable one."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(p)],
            capture_output=True, text=True, timeout=20)
        fmt = json.loads(r.stdout).get("format", {})
        return int(fmt.get("bit_rate") or 0), float(fmt.get("duration") or 0.0)
    except Exception:
        return 0, 0.0


def better(a: Path, b: Path) -> Path:
    """Grey's rule: best quality, then length, longer wins."""
    abr, adur = probe(a)
    bbr, bdur = probe(b)
    if abr != bbr:
        return a if abr > bbr else b
    if abs(adur - bdur) > 1.0:
        return a if adur > bdur else b
    return a          # a is the incumbent; ties keep it, so re-runs are stable


def groups_for(root: Path) -> dict[tuple[str, str], list[Path]]:
    out: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for artist in sorted(p for p in root.iterdir() if p.is_dir()):
        for alb in sorted(p for p in artist.iterdir() if p.is_dir()):
            out[(artist.name, alb.name.casefold())].append(alb)
    return {k: v for k, v in out.items() if len(v) > 1}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    cfg = MusicConfig.from_env()
    libs = Path(cfg.vault_root) / "Libraries"
    renames: list[tuple[Path, Path]] = []      # (old file, new file) for the DB
    n_groups = n_moved = n_lost = 0
    losers: list[tuple[Path, Path, str]] = []

    for tier in TIERS:
        root = libs / tier
        if not root.is_dir():
            continue
        dupes = groups_for(root)
        if not dupes:
            continue
        print(f"\n=== {tier}: {len(dupes)} case-duplicate album(s) ===")
        for (artist, _), paths in sorted(dupes.items()):
            n_groups += 1
            # Which variant to case-correct matters, because album_title_case
            # PRESERVES an interior capital it cannot second-guess -- so
            # "Outlandos D'Amour" and "Outlandos d'Amour" each survive their
            # own spelling and the answer depends entirely on which one is
            # handed over. Taking paths[0] made that sort order, which is
            # arbitrary; the folder holding more tracks is the established
            # spelling and is a reason rather than an accident.
            counts = {p: len(list(p.rglob("*.m4a"))) for p in paths}
            established = max(paths, key=lambda p: (counts[p], p.name))
            want = album_title_case(established.name)
            keep = next((p for p in paths if p.name == want), established)
            print(f"  {artist[:24]:<26}{' | '.join(repr(p.name) for p in paths)}")
            print(f"      -> {want!r}")

            target = keep.parent / want
            for src in paths:
                if src == target:
                    continue
                for f in sorted(x for x in src.rglob("*") if x.is_file()):
                    dst = target / f.relative_to(src)
                    if dst.exists():
                        win = better(dst, f)
                        if win == dst:
                            losers.append((f, dst, "kept incumbent"))
                            n_lost += 1
                            if args.execute:
                                f.unlink()
                            continue
                        losers.append((dst, f, "replaced by better"))
                        n_lost += 1
                        if args.execute:
                            renames.append((dst, dst))   # path unchanged, bytes swapped
                            dst.unlink()
                            f.rename(dst)
                        n_moved += 1
                        continue
                    if args.execute:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        f.rename(dst)
                    renames.append((f, dst))
                    n_moved += 1
                if args.execute:
                    shutil.rmtree(src, ignore_errors=True)
            # the keeper may itself need renaming to the canonical spelling
            if keep.name != want and args.execute and not target.exists():
                keep.rename(target)
            elif keep.name != want and args.execute:
                pass
            if keep.name != want:
                for f in sorted(x for x in (target if args.execute else keep).rglob("*")
                                if x.is_file()):
                    old = keep / f.relative_to(target if args.execute else keep)
                    renames.append((old, f))

    n_rows = 0
    if args.execute and renames:
        conn = sqlite3.connect(cfg.db_path)
        conn.execute("PRAGMA busy_timeout = 60000")
        for old, new in renames:
            if str(old) == str(new):
                continue
            n_rows += conn.execute(
                "UPDATE archive SET file_path=? WHERE file_path=?", (str(new), str(old))
            ).rowcount
            n_rows += conn.execute(
                "UPDATE archive SET car_export_path=? WHERE car_export_path=?",
                (str(new), str(old))
            ).rowcount
        conn.commit()
        conn.close()

    print(f"\n{'DONE' if args.execute else 'DRY RUN'}: {n_groups} group(s), "
          f"{n_moved} file(s) moved, {n_lost} duplicate file(s) resolved, "
          f"{n_rows} database path(s) updated")
    if losers:
        print(f"\n{len(losers)} same-name collision(s) decided by quality then length:")
        for a, b, why in losers[:12]:
            print(f"   {why:<20}{a.parent.name}/{a.name[:44]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
