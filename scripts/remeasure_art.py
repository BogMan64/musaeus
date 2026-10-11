#!/usr/bin/env python3
"""Measure the embedded cover of masters whose stored size is missing or 0.

The catalogue keeps each master's cover size (archive.art_px), and AlbumArt
uses it to find covers too small to keep. A cover changed outside AlbumArt --
the 2026-10-06 cover fixes, a tag editor -- leaves the old size behind: 8
masters read 0 on 2026-10-11 while their covers were 600-1,200 px, which
hides a cover that is really too small. This measures them again from the
file (read-only on the files) and stores the size, one ART_REMEASURED event
per row.

Dry run unless --execute. --execute holds the masters lock.
--all re-measures every master that has a cover, not only missing/0 sizes.

    python3 scripts/remeasure_art.py [--all] [--execute]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.config import get_config  # noqa: E402
from musaeus.stages.albumart import _embedded_art  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--all", action="store_true", help="every master with a cover")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    if args.execute:
        from musaeus.masters_lock import hold_for_process

        hold_for_process(exclusive=True, what="scripts/remeasure_art.py")
    cfg = get_config()
    uri = f"file:{cfg.db_path}" + ("" if args.execute else "?mode=ro")
    db = sqlite3.connect(uri, uri=True)
    where = "has_art = 1" + ("" if args.all else " AND COALESCE(art_px, 0) = 0")
    rows = db.execute(
        f"SELECT file_path, art_px FROM archive WHERE status = 'CATALOGUED' AND {where}"
    ).fetchall()
    run_id = "remeasure_art_" + time.strftime("%Y%m%dT%H%M%S")
    changed = unreadable = 0
    for fp, old in rows:
        seen = _embedded_art(fp)
        if seen is None or not seen[0] or not seen[1]:
            unreadable += 1
            print(f"  could not measure: {fp}")
            continue
        px = int(seen[1])
        if px == (old or 0):
            continue
        changed += 1
        print(f"  {old or 0} -> {px} px  {Path(fp).name}")
        if args.execute:
            db.execute("UPDATE archive SET art_px = ? WHERE file_path = ?", (px, fp))
            db.execute(
                "INSERT INTO events (run_id, event_type, file_path, old_value, new_value, stage) "
                "VALUES (?, 'ART_REMEASURED', ?, ?, ?, 'remeasure_art')",
                (run_id, fp, str(old or 0), str(px)),
            )
    if args.execute:
        db.commit()
    verb = "updated" if args.execute else "would update"
    print(f"{len(rows):,} checked; {verb} {changed:,}; could not measure {unreadable:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
