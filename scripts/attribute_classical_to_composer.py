#!/usr/bin/env python3
"""
File classical recordings under the composer rather than the performer.

The resolution logic lives in musaeus/stages/classical_composer.py and is
imported, not duplicated -- this script is now just an on-demand runner
for a stage that is in the pipeline.

Grey's ruling, 2026-08-24: for classical, the composer is the identity
that matters. A Bach cantata belongs with the other Bach, not scattered
across whichever ensemble happened to record it.

Where the composer comes from
-----------------------------
Not from a tag: **no file in this library carries a composer tag** —
checked, all empty. It appears in two other places, and both are needed:

  1. As one of the comma-separated names in the artist credit
     ("Dubravka Tomsic, Johann Sebastian Bach"). Position varies —
     "Domenico Scarlatti, Dubravka Tomsic" has it first — so it cannot be
     "take the first" or "take the last".
  2. As a prefix on the title ("Handel - Water Music Suite No. 1").

Both are matched EXACTLY against `MetaData/Composer_Canon.tsv`, never as
a substring. That is what keeps these out, all of which are real entries
in this library's classical credits:

    "Franz Liszt Chamber Orchestra"  — an ensemble named after a composer
    "Josef Suk Chamber Orchestra"    — likewise
    "The Four Seasons"               — a work, and a band
    "Fiddler on the Roof"            — a musical

Parsing the title prefix alone was tried first and rejected: it covered
42% and confidently produced "The Four Seasons" (9 tracks) and "Fiddler
on the Roof" (2) as composers.

Anything unresolved is left exactly as it is and listed. A wrong composer
is worse than no composer — it is indistinguishable from a right one once
written, and it moves the file.

Usage:
    python3 scripts/attribute_classical_to_composer.py            # dry run
    python3 scripts/attribute_classical_to_composer.py --apply
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.stages.classical_composer import (  # noqa: E402
    composer_for,
    load_composer_canon,
)
from musaeus.stages.organize import (  # noqa: E402
    build_track_filename,
    sanitize_path_component,
    unique_path,
)

VAULT = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT")
DB = VAULT / "musaeus.db"
CANON = VAULT / "MetaData" / "Composer_Canon.tsv"



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    args = ap.parse_args()

    canon = load_composer_canon(CANON)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, artist, title, file_path FROM archive "
        "WHERE status='CATALOGUED' AND genre='Classical' ORDER BY artist, title"
    ).fetchall()

    resolved, unresolved = [], []
    for r in rows:
        comp, how = composer_for(r["artist"] or "", r["title"] or "", canon)
        if comp and comp != (r["artist"] or "").strip():
            resolved.append((r, comp, how))
        elif not comp:
            unresolved.append(r)

    by_composer = Counter(c for _, c, _ in resolved)
    by_source = Counter(h for _, _, h in resolved)
    print(f"Classical tracks:       {len(rows)}")
    print(f"  composer resolved:    {len(resolved)}   {dict(by_source)}")
    print(f"  already correct:      {len(rows) - len(resolved) - len(unresolved)}")
    print(f"  UNRESOLVED (left as-is): {len(unresolved)}")
    print("\ntracks per composer:")
    for name, n in by_composer.most_common():
        print(f"    {n:>3}  {name}")

    if unresolved:
        print(f"\nunresolved — no canon match in artist or title ({len(unresolved)}):")
        for r in unresolved[:14]:
            print(f"    {(r['artist'] or '')[:40]:<42} {(r['title'] or '')[:42]}")
        if len(unresolved) > 14:
            print(f"    ... and {len(unresolved) - 14} more")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    moved = 0
    for r, comp, _ in resolved:
        src = Path(r["file_path"])
        if not src.exists():
            continue
        album_dir = src.parent
        artist_dir = album_dir.parent
        dst_dir = artist_dir.with_name(sanitize_path_component(comp)) / album_dir.name
        dst = dst_dir / build_track_filename(comp, r["title"] or src.stem, src.suffix)
        # unique_path, because two recordings of the same work by the same
        # composer produce the same filename -- and shutil.move OVERWRITES
        # an existing destination. On 2026-08-24 that destroyed two files
        # before a UNIQUE constraint aborted the run; they were recoverable
        # only from the USB2 mirror.
        dst = unique_path(dst)
        dst_dir.mkdir(parents=True, exist_ok=True)
        conn.execute(
            "UPDATE archive SET artist=?, file_path=? WHERE id=?", (comp, str(dst), r["id"])
        )
        shutil.move(str(src), str(dst))
        conn.execute(
            "INSERT INTO events (run_id,event_type,file_path,old_value,new_value,stage,note) "
            "VALUES (?,?,?,?,?,?,?)",
            ("classical-composer", "ARTIST_SET_TO_COMPOSER", str(dst), r["artist"], comp,
             "manual", "classical filed under composer per owner ruling 2026-08-24"),
        )
        conn.commit()
        conn.commit()
        moved += 1
        for d in (album_dir, artist_dir):
            try:
                d.rmdir()
            except OSError:
                pass
    conn.commit()
    print(f"\napplied: {moved} track(s) refiled under their composer")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
