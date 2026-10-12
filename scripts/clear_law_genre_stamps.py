#!/usr/bin/env python3
"""Clear the genre "rulings" GenreValidate stamped for genres the LAW set.

Grey, 2026-10-11 (review of slice B, finding 4): a MasterLaw change wins over
a genre the law itself filled or corrected; only a genre Grey chose by hand
is frozen. GenreValidate used to stamp genre_ruled_at on every genre it
filled or corrected, which froze them: later MasterLaw changes stopped
reaching them (Jigsaw stayed Rock with the law at Pop; Derek & The Dominos).
It no longer stamps.

Run it only with MasterLaw's credit lines following their lead artist (done
2026-10-11): otherwise the next GenreValidate turns credits Grey set to the
lead's genre on 2026-10-06 back to the old credit lines (SRV & Double Trouble
to Southern Rock).

Until 2026-10-11 only GenreValidate wrote genre_ruled_at, so those stamps
are the law's and this clears them. Since then `musaeus review apply` stamps a
genre Grey approves in a review sheet -- a real ruling. Those rows (a
REVIEW_APPLIED genre event names them) are left alone (pre-merge review of
#180). The next GenreValidate run brings the cleared rows to the current
MasterLaw.

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
        "WHERE genre_ruled_at IS NOT NULL AND TRIM(genre_ruled_at) <> '' "
        "AND file_path NOT IN (SELECT file_path FROM events WHERE "
        "event_type = 'REVIEW_APPLIED' AND note LIKE 'genre (%' AND file_path IS NOT NULL)"
    ).fetchall()
    kept = db.execute(
        "SELECT COUNT(*) FROM archive WHERE genre_ruled_at IS NOT NULL "
        "AND TRIM(genre_ruled_at) <> '' AND file_path IN (SELECT file_path FROM events "
        "WHERE event_type = 'REVIEW_APPLIED' AND note LIKE 'genre (%')"
    ).fetchone()[0]
    artists = {(r["artist"] or "").strip() for r in rows}
    print(f"{len(rows):,} stamped row(s), {len(artists):,} artist(s)")
    if kept:
        print(f"{kept:,} genre(s) Grey approved in a review sheet: kept")
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
    db.executemany(
        "UPDATE archive SET genre_ruled_at = NULL WHERE file_path = ?",
        [(r["file_path"],) for r in rows],
    )
    db.commit()
    print(f"cleared {len(rows):,}; run GenreValidate (Act 1) to apply the current MasterLaw")
    return 0


if __name__ == "__main__":
    sys.exit(main())
