#!/usr/bin/env python3
"""Clear the genre "rulings" GenreValidate stamped for genres the LAW set.

Grey, 2026-10-11 (review of slice B, finding 4): a MasterLaw change wins over
a genre the law itself filled or corrected; only a genre Grey chose by hand
is frozen. GenreValidate used to stamp genre_ruled_at on every genre it
filled or corrected, which froze them: 5 artists ended up split from
MasterLaw (Jigsaw Rock vs law Pop, Derek & The Dominos, Stevie Ray Vaughan &
Double Trouble ...). It no longer stamps.

Every stamp on record was written by GenreValidate -- nothing else in MUSAEUS
has ever written genre_ruled_at (checked through the whole git history) -- so
every one is the law's, and this clears them all. The next GenreValidate run
then brings those rows to the current MasterLaw.

Dry run unless --execute. --execute holds the masters lock, writes one
GENRE_RULING_CLEARED event per row (old_value = the stamp), and commits once.

    python3 scripts/clear_law_genre_stamps.py [--execute]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.config import get_config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    if args.execute:
        from musaeus.masters_lock import hold_for_process

        hold_for_process(exclusive=True, what="scripts/clear_law_genre_stamps.py")

    cfg = get_config()
    uri = f"file:{cfg.db_path}" + ("" if args.execute else "?mode=ro")
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    cols = {r[1] for r in db.execute("PRAGMA table_info(archive)")}
    if "genre_ruled_at" not in cols:
        print("no genre_ruled_at column: nothing stamped")
        return 0
    rows = db.execute(
        "SELECT file_path, artist, genre, genre_ruled_at FROM archive "
        "WHERE genre_ruled_at IS NOT NULL AND TRIM(genre_ruled_at) <> ''"
    ).fetchall()
    artists = {(r["artist"] or "").strip() for r in rows}
    print(f"{len(rows):,} stamped row(s), {len(artists):,} artist(s)")
    if not args.execute:
        print("DRY RUN: nothing changed. --execute clears them; the next GenreValidate")
        print("run then brings those rows to the current MasterLaw.")
        return 0
    run_id = "clear_law_genre_stamps_" + time.strftime("%Y%m%dT%H%M%S")
    for r in rows:
        db.execute(
            "INSERT INTO events (run_id, event_type, file_path, old_value, stage, note) "
            "VALUES (?, 'GENRE_RULING_CLEARED', ?, ?, 'clear_law_genre_stamps', ?)",
            (run_id, r["file_path"], r["genre_ruled_at"],
             f"law-set genre {r['genre']!r} no longer frozen (Grey, 2026-10-11)"),
        )  # fmt: skip
    db.execute("UPDATE archive SET genre_ruled_at = NULL WHERE genre_ruled_at IS NOT NULL")
    db.commit()
    print(f"cleared {len(rows):,}; run GenreValidate (Act 1) to apply the current MasterLaw")
    return 0


if __name__ == "__main__":
    sys.exit(main())
