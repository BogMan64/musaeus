#!/usr/bin/env python3
"""Record the old -18 LUFS baked copies in the catalogue, once.

Grey, 2026-10-29: "If it was baked already, there should be a ref in the
metadata." The retired bake (scripts/alac_library/build_alac_library.py)
stamped lufs_baked_at, but the catalogue was rebuilt on 2026-09-24 and the
stamps were lost. The files carry no mark of their own: their encoder tag,
Lavf59.27.100, is the one every FLAC arrival gets when it is converted.

The keep rule reads only the stamp now (dupe_resolver._looks_baked). This
puts it back on the copies Grey's 2026-09-26 finding named: catalogued
before the originals came in (run bf9647) and measured at -18 LUFS (within
0.3). Nothing catalogued since came from the bake.

    python3 scripts/mark_baked_copies.py            # dry run: lists them
    python3 scripts/mark_baked_copies.py --execute --ids reviewed.txt

Loudness and date are the 2026-09-26 finding, not proof for each file, so
--execute marks only the rows a person reviewed from the dry run (CodeRabbit,
review of #262).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from musaeus.config import MusicConfig  # noqa: E402

EVENT = "BAKED_COPY_MARKED"
#: When the originals of the baked masters were catalogued (run bf9647).
ORIGINALS_CAME_IN = "2026-09-26 16:43:30"
BAKED_LUFS = -18.0
TOLERANCE = 0.3


def candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, file_path, artist, title, status, lufs, date_added FROM archive "
        "WHERE (lufs_baked_at IS NULL OR lufs_baked_at = '') AND lufs IS NOT NULL "
        "AND abs(lufs - ?) <= ? AND date_added < ? ORDER BY id",
        (BAKED_LUFS, TOLERANCE, ORIGINALS_CAME_IN),
    ).fetchall()


def mark(conn: sqlite3.Connection, rows: list[sqlite3.Row], execute: bool) -> None:
    run_id = f"mark_baked_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    for r in rows:
        print(f"  {'MARK' if execute else 'would mark'} id={r['id']} {r['status']} "
              f"{r['artist']} - {r['title']}  ({r['lufs']:.2f} LUFS, added {r['date_added']})")  # fmt: skip
        if not execute:
            continue
        # The bake's own time was lost with the old catalogue: the earliest
        # time this copy is known to have been baked is when it came in.
        conn.execute(
            "UPDATE archive SET lufs_baked_at = date_added, lufs_baked_target = ? WHERE id = ?",
            (BAKED_LUFS, r["id"]),
        )
        conn.execute(
            "INSERT INTO events (run_id, event_type, file_path, old_value, new_value, stage, note) "
            "VALUES (?, ?, ?, NULL, ?, 'mark_baked', ?)",
            (run_id, EVENT, r["file_path"], str(r["id"]),
             f"catalogued before the originals came in, {r['lufs']:.2f} LUFS"),
        )  # fmt: skip
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--execute", action="store_true", help="write the marks (default: dry run)")
    ap.add_argument(
        "--ids",
        metavar="FILE",
        help="mark only these rows, one id per line (needed with --execute)",
    )
    args = ap.parse_args()
    if args.execute and not args.ids:
        ap.error("--execute needs --ids FILE: the rows reviewed from a dry run")
    conn = sqlite3.connect(MusicConfig.from_env().db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000")
    rows = candidates(conn)
    if args.ids:
        reviewed = {int(x) for x in Path(args.ids).read_text().split()}
        rows = [r for r in rows if r["id"] in reviewed]
    mark(conn, rows, args.execute)
    print(f"\n{'MARKED' if args.execute else 'DRY RUN: would mark'} {len(rows):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
