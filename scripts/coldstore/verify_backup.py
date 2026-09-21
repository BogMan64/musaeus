#!/usr/bin/env python3
"""Check a cold backup drive is still intact. Standalone -- needs nothing else.

Read this if you are finding it years later
===========================================
This script lives on the backup drive. It needs only Python 3 (any version
from 3.6) and the drive itself. No MUSAEUS, no database, no internet, no
installation. Copy it anywhere, run it against the drive, and it will tell
you whether the files are still the files.

    python3 verify_backup.py create /path/to/drive/folder
    python3 verify_backup.py verify /path/to/drive/folder

`create` writes BACKUP_MANIFEST.sha256 inside that folder, listing every
file with its SHA-256 and size. Run it once, when the copy is fresh and you
trust it.

`verify` re-reads every file and compares. It reports four things:

    OK        the file is byte-for-byte what it was
    CHANGED   the bytes differ -- silent corruption, or an edit
    MISSING   the manifest lists it and it is not there
    NEW       it is there and the manifest does not list it

Why this exists
---------------
A drive sitting unplugged in a drawer is safe from accidents but not from
physics. Magnetic domains weaken, flash cells leak charge, and a bad sector
develops without anyone reading it. Nothing announces this. The file still
opens, the player still shows the right length, and a few seconds of audio
are quietly wrong -- or the file will not open at all on the one day you
need it.

The only defence is to have written down what the bytes were, while you
still trusted them, and to check occasionally. That is all this does.

How often
---------
Once a year is plenty, and spinning the drive up matters as much as the
check: a disk left unpowered for years can fail on the first spin. A yearly
five minutes turns "hopefully fine" into "verified on this date".

If verify reports CHANGED
-------------------------
The backup is damaged, not the original. Re-copy that file from the live
library. If the live library is gone too, that is what the second backup is
for -- do not overwrite anything until you have found a good copy.

Exit codes: 0 all well, 1 problems found, 2 could not run.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

MANIFEST = "BACKUP_MANIFEST.sha256"
CHUNK = 1024 * 1024


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if name == MANIFEST:
                continue
            p = Path(dirpath) / name
            if p.is_file() and not p.is_symlink():
                yield p


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def create(root: Path) -> int:
    files = list(walk(root))
    total = sum(f.stat().st_size for f in files)
    print(f"Hashing {len(files):,} files ({human(total)}). This reads every byte.")
    lines, done, started = [], 0, time.time()
    for i, f in enumerate(files, 1):
        try:
            digest = sha256(f)
        except OSError as exc:
            print(f"  ! could not read {f}: {exc}")
            continue
        rel = f.relative_to(root).as_posix()
        lines.append(f"{digest}  {f.stat().st_size}  {rel}")
        done += f.stat().st_size
        if i % 200 == 0 or i == len(files):
            pct = 100 * done / total if total else 100
            rate = done / max(time.time() - started, 0.001)
            print(f"  {i:,}/{len(files):,}  {pct:5.1f}%  {human(rate)}/s")
    out = root / MANIFEST
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(f"# Backup manifest written {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        fh.write(f"# {len(lines):,} files, {human(total)}\n")
        fh.write("# sha256  size  path (relative to this folder)\n")
        fh.write("\n".join(lines) + "\n")
    print(f"\nWrote {out}")
    print(f"{len(lines):,} files recorded. Keep this file WITH the backup.")
    return 0


def verify(root: Path) -> int:
    man = root / MANIFEST
    if not man.is_file():
        print(f"No {MANIFEST} in {root}.")
        print("Nothing to compare against -- run `create` when you trust the copy.")
        return 2

    recorded: dict[str, tuple[str, int]] = {}
    for line in man.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        digest, size, rel = line.split("  ", 2)
        recorded[rel] = (digest, int(size))
    print(f"Manifest lists {len(recorded):,} files. Re-reading every byte.\n")

    on_disk = {f.relative_to(root).as_posix(): f for f in walk(root)}
    ok = changed = missing = 0
    problems: list[str] = []
    started = time.time()

    for i, (rel, (digest, size)) in enumerate(sorted(recorded.items()), 1):
        f = on_disk.pop(rel, None)
        if f is None:
            missing += 1
            problems.append(f"MISSING  {rel}")
            continue
        try:
            actual = sha256(f)
        except OSError as exc:
            changed += 1
            problems.append(f"UNREADABLE  {rel}  ({exc})")
            continue
        if actual == digest:
            ok += 1
        else:
            changed += 1
            why = "size differs too" if f.stat().st_size != size else "same size, different bytes"
            problems.append(f"CHANGED  {rel}  ({why})")
        if i % 500 == 0:
            el = time.time() - started
            print(f"  {i:,}/{len(recorded):,} checked  ({el/60:.1f} min)")

    new = sorted(on_disk)
    print("\n" + "=" * 58)
    print(f"  OK       {ok:,}")
    print(f"  CHANGED  {changed:,}")
    print(f"  MISSING  {missing:,}")
    print(f"  NEW      {len(new):,}  (present, not in the manifest)")
    print("=" * 58)

    if problems:
        print("\nProblems:")
        for p in problems[:60]:
            print(f"  {p}")
        if len(problems) > 60:
            print(f"  ... and {len(problems)-60} more")
    for p in new[:20]:
        print(f"  NEW  {p}")

    if not problems and not new:
        print("\nThis backup is intact. Every file is byte-for-byte as recorded.")
        return 0
    if changed or missing:
        print("\nThe BACKUP is damaged, not the original.")
        print("Re-copy the affected files from the live library.")
        print("Do not overwrite anything until you have found a good copy.")
    return 1


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in ("create", "verify"):
        print(__doc__.split("Why this exists")[0].strip())
        return 2
    root = Path(sys.argv[2]).expanduser().resolve()
    if not root.is_dir():
        print(f"Not a folder: {root}")
        return 2
    return create(root) if sys.argv[1] == "create" else verify(root)


if __name__ == "__main__":
    raise SystemExit(main())
