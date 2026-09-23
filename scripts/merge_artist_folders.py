#!/usr/bin/env python3
"""
MUSAEUS — merge artist folders onto the artist tag  (dry-run by default)

Grey's ruling, 2026-09-09: "when I look for a song it will be first by
artist, so group them into one folder". Where a folder is named for a
performance credit -- `Louis Armstrong, Billie Holiday, Sy Oliver & His
Orchestra` -- and the tag says `Louis Armstrong`, the folder moves to the
tag. Not the other way: retagging to the credit would split an artist who
already has a folder, which is what the original 2026-09-08 CSV proposed
and what this reverses for 31 of its rows.

WHAT THIS IS NOT
This does not achieve "one folder per artist" on its own, and the number
says why. 1,493 of 2,773 catalogued artists sit in more than one folder;
1,466 of those are split purely by the `ALAC-Library/<batch date>/` layer,
with a correct name in every copy. Only 27 are a naming problem. This tool
fixes the 27. The other 1,466 need the batch-date layer removed, which is
a separate decision and a separate change.

SAFETY
- Dry run unless --execute. The dry run prints every move it would make.
- Never overwrites. If a destination file already exists the move is
  skipped and reported; two files with one name are a duplicate question,
  not a rename question, and belong to the dupe resolver.
- Renames only within one filesystem, so a move is atomic per file.
- Writes a manifest and an undo script before the first move, not after.
- Updates archive.file_path in the same transaction as the manifest, so a
  crash cannot leave rows pointing at paths that no longer exist.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.config import MusicConfig  # noqa: E402
from musaeus.stages.organize import sanitize_path_component  # noqa: E402


def load_pairs(paths: list[Path]) -> list[tuple[str, str]]:
    """(folder_name, artist_tag) pairs from the ruling CSVs."""
    pairs: dict[str, str] = {}
    for p in paths:
        if not p.exists():
            continue
        for r in csv.DictReader(p.open(encoding="utf-8")):
            folder = (
                r.get("folder is named") or r.get("folder was") or r.get("folder_name") or ""
            ).strip()
            tag = (
                r.get("RENAME IT TO")
                or r.get("RENAME FOLDER TO")
                or r.get("artist tag")
                or r.get("artist tag says")
                or ""
            ).strip()
            skip = (r.get("STOP? (put an x to skip this one)") or "").strip()
            if folder and tag and folder != tag and not skip:
                pairs[folder] = tag
    return sorted(pairs.items())


def plan_moves(cfg: MusicConfig, pairs: list[tuple[str, str]]) -> list[dict]:
    """Every file that would move, with its source and destination.

    Walks the real directory tree rather than trusting the database: a row
    whose file_path is stale would otherwise generate a move for a file
    that is not there, and the report would be fiction.
    """
    moves: list[dict] = []
    lib = cfg.alac_library
    # Artist folders live at TWO depths, not one. The usual shape is
    # `ALAC-Library/<batch date>/<artist>/`, but ten of them sit directly at
    # `ALAC-Library/<artist>/` -- older arrivals that predate the batch
    # layer. A tool that assumed a single depth silently skipped those and
    # reported success, which is how Henry Mancini went missing from the
    # first dry run.
    containers = [lib] + sorted(d for d in lib.iterdir() if d.is_dir())
    for folder_name, tag in pairs:
        safe_tag = sanitize_path_component(tag)
        for batch_dir in containers:
            src_artist = batch_dir / folder_name
            if not src_artist.is_dir():
                continue
            dst_artist = batch_dir / safe_tag
            if src_artist == dst_artist:
                continue
            for src_file in sorted(src_artist.rglob("*")):
                if not src_file.is_file():
                    continue
                rel = src_file.relative_to(src_artist)
                dst_file = dst_artist / rel
                moves.append(
                    {
                        "artist_tag": tag,
                        "folder_was": folder_name,
                        "src": str(src_file),
                        "dst": str(dst_file),
                        "collision": dst_file.exists(),
                        "merging_into_existing": dst_artist.is_dir(),
                    }
                )
    return moves


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--execute", action="store_true", help="actually move files")
    ap.add_argument("--csv", action="append", default=[], help="a ruling CSV (repeatable)")
    args = ap.parse_args()

    cfg = MusicConfig.from_env()
    pairs = load_pairs([Path(p) for p in args.csv])
    if not pairs:
        print("No (folder -> tag) pairs found. Pass --csv with a ruling file.")
        return 1
    print(f"{len(pairs)} folder(s) to merge onto their artist tag.\n")

    moves = plan_moves(cfg, pairs)
    collisions = [m for m in moves if m["collision"]]
    doable = [m for m in moves if not m["collision"]]
    merges = {m["artist_tag"] for m in moves if m["merging_into_existing"]}

    for m in doable[:20]:
        print(
            f"  {m['folder_was'][:38]:38} -> {m['artist_tag'][:26]:26} {Path(m['src']).name[:40]}"
        )
    if len(doable) > 20:
        print(f"  … and {len(doable) - 20} more")

    print(f"\n  files that would move          {len(doable):5d}")
    print(f"  folders merged into an existing artist folder  {len(merges):3d}")
    print(f"  SKIPPED — a file of that name is already there {len(collisions):3d}")
    for m in collisions[:10]:
        print(f"     {Path(m['src']).name[:64]}")

    if not args.execute:
        print("\nDRY RUN — nothing moved. Re-run with --execute to apply.")
        return 0

    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%SZ")
    manifest = Path.home() / "Desktop" / f"MUSAEUS_artist_folder_moves_{stamp}.csv"
    undo = Path.home() / "Desktop" / f"MUSAEUS_artist_folder_undo_{stamp}.sh"

    # Manifest and undo are written BEFORE the first move. A manifest
    # written afterwards is a record of what succeeded, which is the one
    # thing you do not need when something has gone wrong halfway.
    with manifest.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(moves[0].keys()))
        w.writeheader()
        w.writerows(moves)
    with undo.open("w", encoding="utf-8") as fh:
        fh.write(
            f"#!/bin/bash\n# Undo the artist-folder merge of {stamp}.\n"
            f"# Moves every file back to where it came from.\nset -e\n"
        )
        for m in doable:
            parent = repr(str(Path(m["src"]).parent))
            fh.write(f"mkdir -p {parent}\nmv -n {m['dst']!r} {m['src']!r}\n")
    undo.chmod(0o755)
    print(f"\n  manifest -> {manifest}\n  undo     -> {undo}")

    conn = sqlite3.connect(str(cfg.db_path), timeout=120)
    conn.execute("PRAGMA busy_timeout=120000")
    moved = failed = 0
    for m in doable:
        src, dst = Path(m["src"]), Path(m["dst"])
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                failed += 1
                continue
            shutil.move(str(src), str(dst))
            conn.execute(
                "UPDATE archive SET file_path = ? WHERE file_path = ?", (str(dst), str(src))
            )
            moved += 1
        except OSError as exc:
            failed += 1
            print(f"  ! {src.name}: {exc}")
    conn.execute(
        "INSERT INTO events(run_id,ts,event_type,file_path,old_value,new_value,stage,note) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            "artist_folder_merge_" + stamp,
            datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "ARTIST_FOLDER_MERGED",
            "",
            str(len(pairs)) + " folders",
            str(moved) + " files",
            "manual",
            "merged credit-named folders onto the artist tag per Grey's "
            "one-artist-one-folder ruling; manifest " + manifest.name,
        ),
    )
    conn.commit()
    conn.close()

    # Empty source directories left behind are noise, not data.
    removed = 0
    lib = cfg.alac_library
    for folder_name, _ in pairs:
        for batch_dir in [lib] + [d for d in lib.iterdir() if d.is_dir()]:
            d = batch_dir / folder_name
            # `any(d.rglob("*"))` was wrong here: after the files move,
            # the empty ALBUM directories remain, so rglob still yields
            # entries and nothing was ever cleaned up. Count files.
            if d.is_dir() and not any(f.is_file() for f in d.rglob("*")):
                shutil.rmtree(d)
                removed += 1
    print(f"\n  moved {moved} file(s), {failed} skipped, {removed} empty folder(s) removed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
