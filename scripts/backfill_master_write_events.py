#!/usr/bin/env python3
"""Record the 2026-10-06 master writes that logged nothing.

On 2026-10-06 MUSAEUS rewrote the masters' loudness tags
(scripts/write_master_loudness_tags.py, which then logged no events) and
replaced 245 wrong covers, also without events. `musaeus doctor`'s
"changed outside MUSAEUS" check reads a master changed with no MUSAEUS record
as changed outside, so those ~6,680 masters would read as Grey's own changes.

This writes one MASTER_WRITE_BACKFILLED event per master whose file date is
still --day (default 2026-10-06, local date) -- so nothing has written it
since -- with ts = the file's own date and a note saying it was backfilled.
Run again, it skips masters already recorded. Nothing else is touched. Dry run unless --execute, which holds
the masters lock.

    python3 scripts/backfill_master_write_events.py [--day 2026-10-06] [--execute]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.config import get_config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--day", default="2026-10-06", help="local date of the unlogged writes")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    if args.execute:
        from musaeus.masters_lock import hold_for_process

        hold_for_process(exclusive=True, what="scripts/backfill_master_write_events.py")
    cfg = get_config()
    uri = f"file:{cfg.db_path}" + ("" if args.execute else "?mode=ro")
    db = sqlite3.connect(uri, uri=True)
    rows = db.execute("SELECT file_path FROM archive WHERE status = 'CATALOGUED'").fetchall()
    run_id = "backfill_master_write_events_" + time.strftime("%Y%m%dT%H%M%S")
    todo = []
    for (fp,) in rows:
        try:
            mtime = Path(fp).stat().st_mtime
        except OSError:
            continue
        if datetime.fromtimestamp(mtime).strftime("%Y-%m-%d") != args.day:
            continue
        stamp = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if db.execute(
            "SELECT 1 FROM events WHERE file_path = ? AND event_type = 'MASTER_WRITE_BACKFILLED'",
            (fp,),
        ).fetchone():
            continue
        todo.append((fp, stamp))
    print(f"{len(todo):,} master(s) dated {args.day} with no record of the write")
    if not args.execute:
        print("DRY RUN: nothing written. --execute records them.")
        return 0
    for fp, stamp in todo:
        db.execute(
            "INSERT INTO events (run_id, ts, event_type, file_path, stage, note) "
            "VALUES (?, ?, 'MASTER_WRITE_BACKFILLED', ?, 'backfill_master_write_events', ?)",
            (run_id, stamp, fp, f"backfilled 2026-10-11: MUSAEUS wrote this master on "
             f"{args.day} (loudness tags / cover fixes) without an event"),
        )  # fmt: skip
    db.commit()
    print(f"recorded {len(todo):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
