#!/usr/bin/env python3
"""
MUSAEUS -- close the held rows whose files Grey deleted by hand on 2026-11-11.

Grey emptied REVIEW/DUPES_MOVED, REVIEW/TRIBUTE_REMOVED and QUARANTINE in the
file manager at about 10:58 on 2026-11-11 and asked for the rows to be closed.
1,955 archive rows (247 DUPE_REVIEW, 1,708 QUARANTINED) point at files that
are no longer on disk.

This does what HoldExpiryStage._delete does for a held file that has passed
its 30-day hold, minus the unlink (the file is already gone), with the same
record shape and order:

  1. deny the audio hash in hash_index.db, unless the file sat under
     QUARANTINE/corrupted (a damaged file's replacement is wanted), another
     archive row carries the same audio, or the row is a DUPE_REVIEW loser
     whose keeper has left the library (hold-expiry would keep that one; we
     close it but do not deny the audio, and list it);
  2. log one event per row: DELETED_BY_REVIEW (the type the earlier
     delete_reviewed_tracks.py used for files Grey chose to delete), stage
     "hand-close", note saying why;
  3. delete the archive_tier_hashes rows for the path;
  4. delete the archive row.

Both databases are backed up first to _db_backups with the usual
*_pre_<name>_<date>.db names. Every row is re-checked at run time: status
still held, file still missing. Dry run by default; --execute writes.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/mnt/FORGE2TB/Projects/MUSAEUS")
from musaeus.db import deny_hash, ensure_deny_list, log_event, open_hash_index  # noqa: E402
from musaeus.stages.hold_expiry import keeper_in_library  # noqa: E402

VAULT = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT")
DB = VAULT / "musaeus.db"
HASH_INDEX = VAULT / "_db_backups" / "hash_index.db"
BACKUPS = VAULT / "_db_backups"
TAG = "hand_delete_close_20261111"
HELD = ("DUPE_REVIEW", "TRIBUTE_REVIEW", "QUARANTINED")
NOTE = "file removed by hand by Grey on 2026-11-11; row closed on Grey's instruction"


def backup(src: Path, name: str) -> Path:
    dst = BACKUPS / f"{name}_pre_{TAG}.db"
    if dst.exists():
        sys.exit(f"backup already exists, refusing to overwrite: {dst}")
    with sqlite3.connect(str(src)) as s, sqlite3.connect(str(dst)) as d:
        s.backup(d)
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB), timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, status, file_path, audio_hash, artist, title FROM archive "
        "WHERE status IN (?,?,?) ORDER BY id",
        HELD,
    ).fetchall()
    gone = [r for r in rows if not os.path.exists(r["file_path"])]
    still_here = len(rows) - len(gone)
    print(f"held rows {len(rows)}, file missing {len(gone)}, file present {still_here}")
    if still_here:
        print("rows whose file is still on disk are left alone")

    plan = []  # (row, deny, why_not_denied)
    for r in gone:
        h = r["audio_hash"]
        why = ""
        if "/corrupted/" in r["file_path"]:
            why = "corrupted: replacement wanted"
        elif not h:
            why = "no audio hash"
        elif conn.execute(
            "SELECT 1 FROM archive WHERE audio_hash = ? AND id != ? LIMIT 1", (h, r["id"])
        ).fetchone():
            why = "another row carries this audio"
        elif r["status"] == "DUPE_REVIEW" and not keeper_in_library(conn, h):
            why = "keeper has left the library"
        plan.append((r, not why, why))

    n_deny = sum(1 for _, d, _ in plan if d)
    print(f"to close {len(plan)}; deny hash for {n_deny}; not denied {len(plan) - n_deny}")
    for r, _d, why in plan:
        if why == "keeper has left the library":
            print(f"  not denied (keeper gone): id={r['id']} {r['artist']} - {r['title']}")

    if not args.execute:
        print("[DRY] nothing written; rerun with --execute")
        return 0

    print("backups:", backup(DB, "musaeus"), backup(HASH_INDEX, "hash_index"), sep="\n  ")
    run_id = f"{TAG}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:6]}"
    ledger = open_hash_index(HASH_INDEX)
    ensure_deny_list(ledger)
    try:
        for r, deny, _ in plan:
            if deny:
                deny_hash(ledger, r["audio_hash"], NOTE, r["file_path"])
            log_event(
                conn,
                run_id=run_id,
                event_type="DELETED_BY_REVIEW",
                file_path=r["file_path"],
                old_value=r["status"],
                stage="hand-close",
                note=NOTE,
            )
            conn.execute("DELETE FROM archive_tier_hashes WHERE path = ?", (r["file_path"],))
            conn.execute("DELETE FROM archive WHERE id = ?", (r["id"],))
    except (OSError, sqlite3.Error):
        ledger.rollback()
        conn.rollback()
        raise
    ledger.commit()
    conn.commit()
    ledger.close()

    left = conn.execute(
        "SELECT status, COUNT(*) FROM archive GROUP BY status ORDER BY status"
    ).fetchall()
    n_ev = conn.execute("SELECT COUNT(*) FROM events WHERE run_id = ?", (run_id,)).fetchone()[0]
    print(f"done. run_id={run_id} events={n_ev}")
    print("archive now:", ", ".join(f"{s}={n}" for s, n in left))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
