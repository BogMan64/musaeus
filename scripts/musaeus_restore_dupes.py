#!/usr/bin/env python3
"""
MUSAEUS — Selective Duplicate-Move Restore

DupeResolver's own restore_<timestamp>.sh (written alongside every
moved_manifest_<timestamp>.csv in ALAC-Library/DUPES_MOVED_FOR_REVIEW/
<date>/) is all-or-nothing: running it reverses every move in that
batch. This tool works off the SAME manifest CSV but restores only the
rows matching a specific --group (or --file substring), for when you
disagree with one grouping and want to revert just that, not the whole
night's batch.

Usage:
    # List every group_id in a manifest, with counts
    python3 scripts/musaeus_restore_dupes.py --manifest <path.csv> --list

    # Preview what a specific group's restore would do (default: dry run)
    python3 scripts/musaeus_restore_dupes.py --manifest <path.csv> --group near_00069729

    # Actually perform it
    python3 scripts/musaeus_restore_dupes.py --manifest <path.csv> --group near_00069729 --execute

    # Filter by a filename substring instead, if you don't have the group_id handy
    python3 scripts/musaeus_restore_dupes.py --manifest <path.csv> --file "Shake It Up" --execute

Safety:
  - Dry run (no --execute) by default -- prints exactly what would run.
  - Uses the same non-destructive pattern as DupeResolver's own restore
    script: mkdir -p + mv -n (mv -n refuses to overwrite an existing
    file at the source, so a since-recreated file at the original path
    is never silently clobbered).
  - Never touches the DB. This only reverses the physical file move;
    the corresponding archive/duplicates rows are left as DupeResolver
    set them. Re-run DupeResolver (or fix the rows by hand) if you also
    need the DB state to reflect the reverted grouping.
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from collections import Counter
from pathlib import Path


def _load_manifest(manifest_path: Path) -> list[dict]:
    with open(manifest_path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _matching_rows(rows: list[dict], group: str | None, file_substr: str | None) -> list[dict]:
    if group:
        return [r for r in rows if r.get("group_id") == group]
    if file_substr:
        needle = file_substr.lower()
        return [
            r
            for r in rows
            if needle in r.get("source", "").lower() or needle in r.get("destination", "").lower()
        ]
    return []


def _list_groups(rows: list[dict]) -> int:
    counts = Counter(r.get("group_id", "") for r in rows)
    print(f"{len(counts)} group(s) in this manifest:\n")
    for group_id, n in sorted(counts.items()):
        sample = next(r for r in rows if r.get("group_id") == group_id)
        dtype = sample.get("duplicate_type", "?")
        print(f"  {group_id}  ({dtype}, {n} row(s))")
        print(f"    moved:  {Path(sample.get('source', '')).name}")
        kept = sample.get("kept_path", "")
        if kept:
            print(f"    kept:   {Path(kept).name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Selectively restore DupeResolver moves by group")
    parser.add_argument("--manifest", required=True, type=Path, help="Path to moved_manifest_*.csv")
    parser.add_argument("--group", help="Restore only this group_id")
    parser.add_argument("--file", help="Restore only rows whose source/destination contains this substring")
    parser.add_argument("--list", action="store_true", help="List every group_id in the manifest and exit")
    parser.add_argument("--execute", action="store_true", help="Actually perform the restore (default: dry run)")
    args = parser.parse_args()

    if not args.manifest.exists():
        print(f"ERROR: manifest not found: {args.manifest}", file=sys.stderr)
        return 1

    rows = _load_manifest(args.manifest)

    if args.list:
        return _list_groups(rows)

    if not args.group and not args.file:
        print("ERROR: specify --group <id>, --file <substring>, or --list", file=sys.stderr)
        return 1

    matches = _matching_rows(rows, args.group, args.file)
    if not matches:
        print("No matching rows found.", file=sys.stderr)
        return 1

    mode = "EXECUTING" if args.execute else "DRY RUN (pass --execute to actually perform this)"
    print(f"{mode} — {len(matches)} row(s) matched:\n")

    errors = 0
    for row in matches:
        src, dst = row["source"], row["destination"]
        kept = row.get("kept_path", "")
        print(f"  [{row.get('duplicate_type', '?')}] {Path(dst).name}")
        print(f"    restore to: {src}")
        if kept:
            print(f"    (was kept over: {Path(kept).name})")

        if not args.execute:
            continue

        dst_path = Path(dst)
        if not dst_path.exists():
            print(f"    ⚠ SKIPPED — moved file no longer exists at {dst}", file=sys.stderr)
            errors += 1
            continue

        src_path = Path(src)
        if src_path.exists():
            print(f"    ⚠ SKIPPED — something already exists at the restore target {src}", file=sys.stderr)
            errors += 1
            continue

        try:
            src_path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["mv", "-n", str(dst_path), str(src_path)], check=True)
            print("    ✓ restored")
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"    ✗ FAILED: {exc}", file=sys.stderr)
            errors += 1

    if args.execute:
        print(f"\n{len(matches) - errors} restored, {errors} error(s).")
        print("Note: DB rows (archive.status, duplicates.status) were NOT changed by this tool.")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
