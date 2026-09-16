#!/usr/bin/env python3
"""File CAR_Library artist folders under the sort form, as the other tiers are.

WHY THIS EXISTS, AND WHY IT IS NOT IN THE ENCODER
--------------------------------------------------
Grey's filing rule: the article goes at the END of an artist FOLDER, so the
library browses under B rather than under T. ALAC-Archival and ALAC_Library
obey it -- 0 of ~3,000 folders start with "The " in either. CAR_Library had
63 that did, because it derives its folder from the file's TAGS, and the tag
is deliberately natural form ("The Beatles") -- that is the whole point of
the 2026-09-16 article migration, which moved archive.artist to the form
external services can read.

So the tag being natural is correct, and the folder being natural is not.
Three fields, three jobs.

The conversion cannot live in `build_aac_library.py`: that encoder is
vendored from ORPHEUS and must keep running outside MUSAEUS, which is why it
has never imported musaeus -- `_duration_tolerance` mirrors
`musaeus/duration.py` by hand rather than import it. Article filing is a
MUSAEUS ruling, so it belongs in MUSAEUS code, applied after publish.

MERGING, NOT JUST RENAMING
--------------------------
`publish_edition` merges into CAR_Library rather than clearing it, so a
"Beatles, The" folder from an older build can already exist beside a new
"The Beatles". Renaming blindly would fail on the collision, and worse, a
build that half-renamed would leave one artist filed in two places -- which
is the split-artist failure the filing rules exist to prevent. Contents are
merged, and a file that would overwrite a different file is left alone and
reported rather than silently replaced.

    python3 scripts/car_library/normalize_car_artist_folders.py            # dry run
    python3 scripts/car_library/normalize_car_artist_folders.py --execute
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from musaeus.artist_form import sort_form  # noqa: E402
from musaeus.config import MusicConfig  # noqa: E402


def folders_needing_a_move(root: Path) -> list[tuple[Path, Path]]:
    """(current, wanted) for every artist folder not in sort form."""
    moves = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        wanted = sort_form(d.name)
        if wanted and wanted != d.name:
            moves.append((d, d.parent / wanted))
    return moves


def merge_dir(src: Path, dst: Path, execute: bool) -> tuple[int, list[str]]:
    """Move every file from *src* into *dst*. Returns (moved, conflicts)."""
    moved, conflicts = 0, []
    for item in sorted(src.rglob("*")):
        if item.is_dir():
            continue
        rel = item.relative_to(src)
        target = dst / rel
        if target.exists():
            # Same size is almost certainly the same encode published twice.
            # Anything else is two different files claiming one name, and that
            # is not a thing to resolve silently.
            if target.stat().st_size == item.stat().st_size:
                if execute:
                    item.unlink()
                moved += 1
            else:
                conflicts.append(str(rel))
            continue
        if execute:
            target.parent.mkdir(parents=True, exist_ok=True)
            item.rename(target)
        moved += 1
    return moved, conflicts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="actually move (default: dry run)")
    ap.add_argument("--root", type=Path, default=None, help="CAR_Library root")
    args = ap.parse_args()

    cfg = MusicConfig.from_env()
    root = args.root or Path(cfg.vault_root) / "Libraries" / "CAR_Library"
    if not root.is_dir():
        print(f"no CAR_Library at {root}")
        return 1

    moves = folders_needing_a_move(root)
    if not moves:
        print(f"{root.name}: every artist folder is already in sort form.")
        return 0

    print(f"{root.name}: {len(moves)} folder(s) to file under the sort form\n")
    n_merged = n_renamed = 0
    all_conflicts: list[str] = []
    for src, dst in moves:
        if dst.exists():
            moved, conflicts = merge_dir(src, dst, args.execute)
            all_conflicts += [f"{dst.name}/{c}" for c in conflicts]
            print(f"  MERGE  {src.name!r} -> {dst.name!r}  ({moved} file(s)"
                  f"{f', {len(conflicts)} CONFLICT' if conflicts else ''})")
            if args.execute and not conflicts:
                for d in sorted((p for p in src.rglob("*") if p.is_dir()), reverse=True):
                    if not any(d.iterdir()):
                        d.rmdir()
                if not any(src.iterdir()):
                    src.rmdir()
            n_merged += 1
        else:
            print(f"  RENAME {src.name!r} -> {dst.name!r}")
            if args.execute:
                src.rename(dst)
            n_renamed += 1

    # The catalogue points at these files by absolute path. A rename that does
    # not carry car_export_path with it turns every moved row into a phantom.
    n_rows = 0
    if args.execute:
        conn = sqlite3.connect(cfg.db_path)
        for src, dst in moves:
            cur = conn.execute(
                "UPDATE archive SET car_export_path = replace(car_export_path, ?, ?) "
                "WHERE car_export_path LIKE ?",
                (f"/{src.name}/", f"/{dst.name}/", f"%/{src.name}/%"))
            n_rows += cur.rowcount
        conn.commit()
        conn.close()

    print(f"\n{'DONE' if args.execute else 'DRY RUN'}: "
          f"{n_renamed} renamed, {n_merged} merged, {n_rows} car_export_path row(s) updated")
    if all_conflicts:
        print(f"\n{len(all_conflicts)} CONFLICT(S) left in place -- different files, same name:")
        for c in all_conflicts[:20]:
            print(f"   {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
