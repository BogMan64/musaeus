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
import filecmp
import shutil
import sqlite3
import sys

import mutagen.mp4
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
            # The same file filed twice goes; anything else is two different
            # recordings claiming one name and is left for a person. Size alone
            # used to decide that; a byte comparison settles it, because the
            # rare wrong answer deletes a recording that exists nowhere else.
            if target.stat().st_size == f.stat().st_size and filecmp.cmp(target, f, shallow=False):
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


def _write_canon_entry(canon: Path, old: str, new: str) -> int:
    """Add old -> new, and re-point anything that would now chain through it.

    resolve_exact does NOT follow a chain. So if some existing row already
    says `X -> old`, adding `old -> new` leaves X landing on `old` and
    stopping there -- half-way, at a name nothing else uses. doctor's
    "authorities agree" check calls that a FAIL, and it is right to.

    It happened the first time this script ran. Merging ELO into Electric
    Light Orchestra turned a pre-existing "Jeff Lynne's ELO" -> "ELO" into a
    chain, and doctor went red on the next run. Nothing was mis-filed --
    both names had zero tracks -- but the ruling file was inconsistent and
    the next artist to arrive under that name would have landed wrong.

    Returns how many existing entries had to be re-pointed.
    """
    text = canon.read_text(encoding="utf-8")
    lines = text.splitlines()

    repointed = 0
    for i, ln in enumerate(lines):
        parts = ln.split("\t", 1)
        if len(parts) == 2 and parts[1].strip() == old:
            lines[i] = f"{parts[0]}\t{new}"
            repointed += 1

    # And never write a row whose canonical is itself a key -- the same
    # chain, created in the other direction.
    keys = {ln.split("\t", 1)[0].strip().lower()
            for ln in lines if "\t" in ln and not ln.startswith("#")}
    if new.lower() in keys:
        dest = next(ln.split("\t", 1)[1].strip() for ln in lines
                    if "\t" in ln and ln.split("\t", 1)[0].strip().lower() == new.lower())
        print(f"  note: {new!r} is itself a canon key pointing at {dest!r}; "
              f"writing {old!r} -> {dest!r} instead")
        new = dest

    # Whole-key comparison, not `f"{old}\t" in text`. That substring test
    # reports a match when `old` is merely the TAIL of another key:
    # "Jeff Lynne's ELO\tELO" contains "ELO\t", so adding ELO was silently
    # skipped and the merge left no canon entry at all.
    if old.lower() not in keys:
        lines.append(f"{old}\t{new}")
        print(f"  artist_canon.tsv: added {old!r} -> {new!r}")

    canon.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return repointed


def move_car_files_by_db(cfg, old: str, new: str, execute: bool) -> list[tuple[Path, Path]]:
    """Relocate the CAR tier by asking the database, not by guessing the folder.

    The other two tiers file an artist under sort_form(artist_tag), so walking
    `Libraries/<tier>/<sort_form(old)>` finds them. **The car tier does not.**
    It files under the ALBUM-ARTIST tag, which is frequently a different
    string: merging "Daryl Hall & John Oates" into "Hall & Oates" on
    2026-09-17 found 16 files in each ALAC tier and none in the car, because
    the car folder was called "Daryl Hall".

    The tier loop skips a directory that does not exist, so this failed
    silently and reported `0 car_export_path updated` -- which reads exactly
    like "there were none". Every artist merge before that date may have left
    the same residue.

    So the car tier is resolved by `car_export_path` on the rows being
    renamed, which is the only authority that knows where those files
    actually are.
    """
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, car_export_path FROM archive "
        "WHERE artist = ? AND car_export_path IS NOT NULL AND TRIM(car_export_path) != ''",
        (old,),
    ).fetchall()
    car_root = Path(cfg.vault_root) / "Libraries" / "CAR_Library"
    folder = sort_form(new)
    moves: list[tuple[Path, Path]] = []
    for row in rows:
        src = Path(row["car_export_path"])
        if not src.is_file():
            continue
        if src.parent.parent == car_root / folder:
            continue  # already filed correctly
        stem = src.name
        renamed = f"{folder} - {stem.split(' - ', 1)[1]}" if " - " in stem else stem
        dst = car_root / folder / src.parent.name / renamed
        if dst.exists():
            continue  # a clash is the caller's problem, not this function's
        moves.append((src, dst))
        if execute:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            tags = mutagen.mp4.MP4(dst)
            if tags.tags is None:
                tags.add_tags()
            tags.tags["aART"] = [new]
            tags.save()
            conn.execute(
                "UPDATE archive SET car_export_path = ? WHERE id = ?", (str(dst), row["id"])
            )
    if execute:
        conn.commit()
    conn.close()
    return moves


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

    # The car tier, resolved from the database rather than from a folder name.
    # Runs BEFORE the artist rename below, because it selects on the OLD name.
    car_moves = move_car_files_by_db(cfg, args.old, args.new, args.execute)
    print(f"  CAR_Library (by car_export_path): {len(car_moves)} file(s) moved")

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
            n_chain = _write_canon_entry(canon, args.old, args.new)
            if n_chain:
                print(f"  artist_canon.tsv: re-pointed {n_chain} entry(ies) that would "
                      f"otherwise chain through {args.old!r}")

    print(f"\n{'DONE' if args.execute else 'DRY RUN'}: {len(all_moves)} file(s), "
          f"{n_art} artist row(s), {n_fp} file_path, {n_car} car_export_path updated")
    if all_clashes:
        print(f"\n{len(all_clashes)} CLASH(ES) left in place -- different files, same name:")
        for c in all_clashes[:10]:
            print(f"   {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
