#!/usr/bin/env python3
"""Re-file every CAR_Library track under the folder the CATALOGUE implies.

WHY normalize_car_artist_folders.py IS NOT ENOUGH
-------------------------------------------------
That script fixes one thing: the article rule ("The Beatles" -> "Beatles,
The"). Measured 2026-09-17 it reports "every artist folder is already in sort
form", and it is right. But a sweep the same day found **412 car folders
holding 952 files that no catalogued artist maps to**, in five families it
cannot see:

    case            Ac-dc / AC-DC      NAZARETH / Nazareth   Tlc / TLC
    double comma    "Beach Boys,, The"  "Cure,, The"
    & truncation    "Jan" for Jan & Dean, "Kool" for Kool & The Gang
    band-part case  "Doug & the Slugs" vs the tag "Doug & The Slugs"
    accents         "Beyonce" / Beyoncé, "VanessaMae" / Vanessa-Mae

The common cause is that the car folder is derived from the FILE'S TAGS at
encode time, by a vendored encoder that has never imported musaeus. Every
ruling made in the catalogue since a file was encoded -- an artist merge, a
canon entry, a filing rule -- is invisible to it.

So this does not try to repair folder names. It asks the catalogue where each
file belongs and puts it there. `car_export_path` is the authority for where a
file IS; `filing.folder_for(sort_form(artist))` is the authority for where it
SHOULD be.

A file whose destination already exists is LEFT ALONE and reported, never
overwritten -- the same rule normalize_car_artist_folders.py uses, for the
same reason: a half-done move leaves one artist filed in two places, which is
the split this is meant to end.

    python3 scripts/car_library/refile_car_by_catalogue.py            # dry run
    python3 scripts/car_library/refile_car_by_catalogue.py --execute
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import mutagen.mp4

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from musaeus import filing  # noqa: E402
from musaeus.artist_form import sort_form  # noqa: E402
from musaeus.config import MusicConfig  # noqa: E402
from musaeus.stages.organize import sanitize_path_component  # noqa: E402


def plan(cfg: MusicConfig) -> tuple[list[tuple[Path, Path, str]], list[str]]:
    """Return (moves, problems). Read-only."""
    rules = filing.load(Path(cfg.meta_dir))
    car_root = Path(cfg.vault_root) / "Libraries" / "CAR_Library"
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, artist, car_export_path FROM archive "
        "WHERE status = 'CATALOGUED' AND car_export_path IS NOT NULL "
        "AND TRIM(car_export_path) != ''"
    ).fetchall()
    conn.close()

    moves: list[tuple[Path, Path, str]] = []
    problems: list[str] = []
    claimed: set[Path] = set()
    # A car file may be shared by more than one catalogued row -- 77 are, and
    # that is deliberate: near-duplicate recordings legitimately point at one
    # encode. Planning per ROW therefore queues the same source twice, and the
    # second move finds it already gone. Plan per SOURCE FILE instead.
    seen_src: set[Path] = set()
    for row in rows:
        src = Path(row["car_export_path"])
        if not src.is_file():
            problems.append(f"row {row['id']}: car file missing: {src}")
            continue
        # filing rules are keyed on the TAG, and the article rule applies to
        # whatever comes out of them -- rule first, then sort form.
        # sanitize_path_component, not the raw name: two artists in this
        # catalogue contain a path separator -- "M/a/r/r/s" and
        # "Morse/portnoy/george". The ALAC tiers file them as "M-a-r-r-s" and
        # "Morse-portnoy-george"; an unsanitised name instead builds a nested
        # directory and the track lands in a folder called "r".
        want_folder = sanitize_path_component(sort_form(filing.folder_for(row["artist"], rules)))
        album = src.parent.name
        stem = src.name
        renamed = (
            f"{want_folder} - {stem.split(' - ', 1)[1]}" if " - " in stem else stem
        )
        dst = car_root / want_folder / album / renamed
        if dst == src:
            continue
        if src in seen_src:
            continue  # already queued by an earlier row that shares this file
        seen_src.add(src)
        if dst.exists() or dst in claimed:
            problems.append(f"row {row['id']}: destination taken, left alone: {dst}")
            continue
        claimed.add(dst)
        moves.append((src, dst, row["artist"] or ""))
    return moves, problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="actually move (default: dry run)")
    args = ap.parse_args()

    cfg = MusicConfig.from_env()
    moves, problems = plan(cfg)
    car_root = Path(cfg.vault_root) / "Libraries" / "CAR_Library"

    print(f"  files to re-file : {len(moves)}")
    print(f"  problems         : {len(problems)}")
    folders = Counter(str(d.relative_to(car_root).parts[0]) for _, d, _ in moves)
    print(f"  destination folders touched: {len(folders)}\n")
    for src, dst, _ in moves[:15]:
        print(f"    {src.relative_to(car_root)}")
        print(f"      -> {dst.relative_to(car_root)}")
    if len(moves) > 15:
        print(f"    ... and {len(moves) - 15} more")
    for p in problems[:10]:
        print(f"    PROBLEM {p}")

    if not args.execute:
        print("\n  DRY RUN — nothing moved. Re-run with --execute.")
        return 0

    conn = sqlite3.connect(cfg.db_path)
    conn.execute("PRAGMA busy_timeout = 60000")
    done = 0
    skipped = 0
    for src, dst, artist in moves:
        if not src.is_file():
            skipped += 1   # a partial earlier run already moved it
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        tags = mutagen.mp4.MP4(dst)
        if tags.tags is None:
            tags.add_tags()
        # The album-artist tag is what the encoder filed by, so leaving it
        # wrong means the next build undoes this.
        tags.tags["aART"] = [artist]
        tags.save()
        conn.execute("UPDATE archive SET car_export_path = ? WHERE car_export_path = ?",
                     (str(dst), str(src)))
        # Commit per move, not at the end. The first run of this script raised
        # on move 419 of 1,171; the loop's UPDATEs were still uncommitted, so
        # 412 files had moved on disk while every row still pointed at the old
        # path. The move is not transactional with the database, so the window
        # between them has to be one file wide, not the whole run.
        conn.commit()
        done += 1
    conn.close()
    for d in sorted(car_root.rglob("*"), key=lambda p: -len(p.parts)):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    print(f"\n  MOVED {done} file(s)"
          + (f"; {skipped} source(s) already gone (earlier partial run)" if skipped else "")
          + "; empty folders pruned.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
