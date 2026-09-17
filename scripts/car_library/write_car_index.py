#!/usr/bin/env python3
"""
MUSAEUS — write the car edition's browsing index.

Grey asked (2026-09-17) whether an index can be written for the Android head
unit, and made standard on CAR_Library.

**There is no index format that stops a head unit scanning.** Android units
build their own media database from a MediaStore scan on insert; FAT32 has no
directory index, so the scan is a linear walk of ~10,650 files however the
stick is arranged. Nothing MUSAEUS writes changes that.

What IS portable, and what this writes, is **M3U8 playlists**: per genre, per
decade, and one All. Every mainstream Android player (the stock unit apps,
Poweramp, VLC, Kodi) reads them, they give a browse-by-genre view that does
not depend on the unit's own database being correct, and they cost a few
hundred kilobytes.

It writes them INSIDE the edition -- `CAR_Library/Playlists/` -- for one
reason: the edition is what gets copied to the stick, so an index that lives
anywhere else is an index that does not travel.

It reuses `PlaylistStage`, which already groups by genre and decade and emits
`#EXTINF` lines. Pointing that stage at `CAR_Library/Playlists` makes its
relative paths come out as `../Artist/Album/Track.m4a`, which resolves both in
the vault and on the USB, because the playlist folder sits one level below the
edition root in both places.

Usage:
    python3 scripts/car_library/write_car_index.py            # dry run
    python3 scripts/car_library/write_car_index.py --apply
"""

from __future__ import annotations

import argparse
import dataclasses
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from musaeus.config import MusicConfig  # noqa: E402
from musaeus.context import RunContext  # noqa: E402
from musaeus.stages.playlist import PlaylistStage  # noqa: E402

#: Where the index lives inside the edition. One level below the edition root,
#: which is what makes the "../" in every entry resolve on the USB too.
INDEX_DIRNAME = "Playlists"


def index_dir_for(edition_root: Path) -> Path:
    return edition_root / INDEX_DIRNAME


def verify_index(index_dir: Path) -> list[str]:
    """Open every playlist and check each entry resolves to a real file.

    Existence of the .m3u8 is not evidence it works -- a playlist whose lines
    point nowhere looks identical to one that does, and only reports itself in
    the car. So the entries are resolved here, against the disk.
    """
    problems: list[str] = []
    playlists = sorted(index_dir.glob("*.m3u8"))
    if not playlists:
        return [f"no playlists were written to {index_dir}"]
    for pl in playlists:
        entries = [
            ln.strip()
            for ln in pl.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")
        ]
        if not entries:
            problems.append(f"{pl.name}: no entries")
            continue
        for entry in entries:
            if Path(entry).is_absolute():
                problems.append(f"{pl.name}: absolute path, will not survive the USB: {entry}")
                break
            if not (pl.parent / entry).resolve().is_file():
                problems.append(f"{pl.name}: entry does not resolve: {entry}")
                break
    return problems


def write_index(cfg: MusicConfig, edition_root: Path, apply: bool) -> tuple[list[str], list[str]]:
    """Write (or dry-run) the index for *edition_root*.

    Returns (notes, problems). `problems` is empty only when the index was
    written AND every entry in it was resolved against the disk -- existence
    of a .m3u8 proves nothing, which is the rule the rest of this codebase
    already runs on.
    """
    index_dir = index_dir_for(edition_root)
    # dataclasses.replace, not mutation: the stage reads cfg.playlists and
    # nothing else should see this override.
    cfg = dataclasses.replace(cfg, playlists=index_dir)

    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    ctx = RunContext.new(cfg, conn, dry_run=not apply)
    try:
        stage = PlaylistStage()
        stage.validate(ctx)
        result = stage.run(ctx) if apply else stage.dry_run(ctx)
        notes = list(result.notes)
    finally:
        ctx.finish()
        conn.close()

    problems = verify_index(index_dir) if apply else []
    return notes, problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry run")
    ap.add_argument("--edition", default="CAR_Library", help="edition folder name")
    args = ap.parse_args()

    cfg = MusicConfig.from_env()
    edition_root = Path(cfg.libraries) / args.edition
    if not edition_root.is_dir():
        print(f"ERROR: no such edition: {edition_root}")
        return 1

    index_dir = index_dir_for(edition_root)
    print(f"   edition : {edition_root}")
    print(f"   index   : {index_dir}")
    print(f"   mode    : {'APPLY' if args.apply else 'DRY RUN'}\n")

    notes, problems = write_index(cfg, edition_root, apply=args.apply)
    for note in notes:
        print(f"   {note}")

    if not args.apply:
        print("\n   Nothing was written. Re-run with --apply.")
        return 0

    if problems:
        print(f"\n   VERIFY FAILED ({len(problems)}):")
        for p in problems[:20]:
            print(f"     {p}")
        return 1
    n = len(list(index_dir.glob("*.m3u8")))
    print(f"\n   verified: {n} playlist(s), every entry resolves, none absolute")
    return 0


if __name__ == "__main__":
    sys.exit(main())
