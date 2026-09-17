#!/usr/bin/env python3
"""Merge an artist filed under two names into one, folders and catalogue together.

Grey, 2026-09-16: "we do not need to edit the music tracks just the artist
folder for searching." So this moves FOLDERS and updates the CATALOGUE. It
never rewrites a tag -- the files are left byte-identical.

WHY THIS IS NOT JUST `mv`

    archive.artist          what every lookup and every report reads
    archive.file_path       absolute; a folder move orphans it
    archive.car_export_path absolute; same
    artist_canon.tsv        what stops the split reappearing on the next ingest

A rename that carries only some of those produces a catalogue pointing at
files that are not there -- the phantom-row failure this library has had to
repair twice in one day. All four move together or the merge is not done.

FOLDERS ARE SORT FORM, TAGS ARE NATURAL FORM

"The English Beat" files under "English Beat, The". The canonical name given
on the command line is the TAG form; the folder name is derived with
sort_form, the same call organize.py makes. Passing a folder name by hand is
how the two conventions drift apart.

    python3 scripts/consolidate_artist_folders.py "John Cougar Mellencamp" "John Mellencamp"
    python3 scripts/consolidate_artist_folders.py "John Cougar Mellencamp" "John Mellencamp" --execute
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from musaeus.artist_form import sort_form  # noqa: E402
from musaeus.config import MusicConfig  # noqa: E402

TIERS = ("ALAC-Archival", "ALAC_Library", "CAR_Library")


def merge_tree(src: Path, dst: Path, execute: bool) -> tuple[list[tuple[Path, Path]], list[str]]:
    """Move every file from *src* under *dst*, keeping the album layer."""
    moves: list[tuple[Path, Path]] = []
    clashes: list[str] = []
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        target = dst / f.relative_to(src)
        if target.exists():
            # Same size is the same file filed twice; anything else is two
            # different recordings claiming one name and is left for a person.
            if target.stat().st_size == f.stat().st_size:
                if execute:
                    f.unlink()
                continue
            clashes.append(str(f.relative_to(src)))
            continue
        if execute:
            target.parent.mkdir(parents=True, exist_ok=True)
            f.rename(target)
        moves.append((f, target))
    return moves, clashes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old", help="the artist name to retire (tag form)")
    ap.add_argument("new", help="the artist name to keep (tag form)")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    cfg = MusicConfig.from_env()
    libs = Path(cfg.vault_root) / "Libraries"
    old_dir, new_dir = sort_form(args.old), sort_form(args.new)
    print(f"  {args.old!r} -> {args.new!r}")
    print(f"  folders: {old_dir!r} -> {new_dir!r}\n")

    all_moves: list[tuple[Path, Path]] = []
    all_clashes: list[str] = []
    for tier in TIERS:
        src, dst = libs / tier / old_dir, libs / tier / new_dir
        if not src.is_dir():
            continue
        moves, clashes = merge_tree(src, dst, args.execute)
        all_moves += moves
        all_clashes += [f"{tier}/{c}" for c in clashes]
        print(f"  {tier}: {len(moves)} file(s) moved"
              + (f", {len(clashes)} CLASH" if clashes else ""))
        if args.execute and not clashes:
            shutil.rmtree(src, ignore_errors=True)

    n_art = n_fp = n_car = 0
    if args.execute:
        conn = sqlite3.connect(cfg.db_path)
        conn.execute("PRAGMA busy_timeout = 60000")
        n_art = conn.execute("UPDATE archive SET artist=? WHERE artist=?",
                             (args.new, args.old)).rowcount
        for src, dst in all_moves:
            n_fp += conn.execute("UPDATE archive SET file_path=? WHERE file_path=?",
                                 (str(dst), str(src))).rowcount
            n_car += conn.execute("UPDATE archive SET car_export_path=? WHERE car_export_path=?",
                                  (str(dst), str(src))).rowcount
        conn.commit()
        conn.close()

        # The canon is what stops the split coming back on the next ingest.
        canon = Path(cfg.meta_dir) / "artist_canon.tsv"
        if canon.is_file():
            text = canon.read_text(encoding="utf-8")
            if f"{args.old}\t" not in text:
                with canon.open("a", encoding="utf-8") as fh:
                    if not text.endswith("\n"):
                        fh.write("\n")
                    fh.write(f"{args.old}\t{args.new}\n")
                print(f"  artist_canon.tsv: added {args.old!r} -> {args.new!r}")

    print(f"\n{'DONE' if args.execute else 'DRY RUN'}: {len(all_moves)} file(s), "
          f"{n_art} artist row(s), {n_fp} file_path, {n_car} car_export_path updated")
    if all_clashes:
        print(f"\n{len(all_clashes)} CLASH(ES) left in place -- different files, same name:")
        for c in all_clashes[:10]:
            print(f"   {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
