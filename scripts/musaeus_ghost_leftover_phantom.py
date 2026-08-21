#!/usr/bin/env python3
"""
MUSAEUS — Mark leftover orphaned phantom rows as GHOST (2026-08-14 incident, phase 3)

Context: phase 1 (musaeus_relink_recovered_dupes.py) recovered 10,644 rows by
relinking freshly re-ingested copies. Phase 2 (musaeus_ghost_confirmed_gone.py)
marked the 4,608 original rows with zero live copy anywhere as GHOST. That
left a third population unaccounted for: of the original 14,467 phantom
CATALOGUED rows pointing at the vanished ALAC-Library/2026-08-12 directory,
14,467 - 4,608 = 9,859 have a live duplicate elsewhere (via their relinked
counterpart) but are themselves still status='CATALOGUED' pointing at a path
that no longer exists -- orphaned original pointers for content that now
lives under a different archive row.

Scope: since phase 2 already moved the zero-live-copy rows to GHOST and
DUPE_REVIEW rows are separately confirmed to have zero missing files, the
entire remaining "CATALOGUED but missing on disk" population *is* exactly
this leftover set -- no additional hash-matching needed to isolate it.
Verified read-only before writing this script: 9,859 rows, all
status='CATALOGUED', all missing on disk, zero overlap with 'GHOST' or
'DUPE_REVIEW' rows by construction.

What this script does: same as phase 2 -- status='GHOST', GHOST_FOUND event,
no files touched. Nothing on disk changes; this is a pure DB label pass.

Safety: dry run by default; every row re-verified live at run time (status
still CATALOGUED, file still missing) before being touched.

Usage:
    python3 scripts/musaeus_ghost_leftover_phantom.py            # dry run
    python3 scripts/musaeus_ghost_leftover_phantom.py --execute  # write
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

VAULT_ROOT = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT")
DB_PATH = VAULT_ROOT / "musaeus.db"

_COMMIT_EVERY = 500


def _leftover_phantom_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute("SELECT id, file_path FROM archive WHERE status='CATALOGUED'").fetchall()
    return [r["id"] for r in rows if not Path(r["file_path"]).exists()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="Actually write DB changes (default: dry run)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    ids = sorted(_leftover_phantom_ids(conn))
    print(f"{'EXECUTING' if args.execute else 'DRY RUN'} — {len(ids)} leftover orphaned-phantom row(s)\n")

    marked = 0
    skipped = 0
    for i, rowid in enumerate(ids):
        row = conn.execute(
            "SELECT id, status, file_path FROM archive WHERE id = ?", (rowid,)
        ).fetchone()

        if row is None:
            print(f"  SKIP id={rowid}: no longer in archive")
            skipped += 1
            continue
        if row["status"] != "CATALOGUED":
            print(f"  SKIP id={rowid}: status is {row['status']!r}, not CATALOGUED (already handled)")
            skipped += 1
            continue
        if Path(row["file_path"]).exists():
            print(f"  SKIP id={rowid}: file now exists on disk (recovered since scoping) — {row['file_path']}")
            skipped += 1
            continue

        if i < 10:
            print(f"  id={rowid:<6} CATALOGUED -> GHOST   {row['file_path']}")

        if args.execute:
            conn.execute(
                "UPDATE archive SET status='GHOST', last_seen=datetime('now') WHERE id=?",
                (rowid,),
            )
            conn.execute(
                "INSERT INTO events (run_id, event_type, file_path, old_value, new_value, stage, note) "
                "VALUES (?, 'GHOST_FOUND', ?, ?, 'GHOST', 'ghost-leftover-phantom-manual', ?)",
                (
                    f"ghost_{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
                    row["file_path"],
                    row["status"],
                    "orphaned original pointer: live duplicate recovered elsewhere, this row's own path is gone (2026-08-14 incident phase 3)",
                ),
            )
            if marked % _COMMIT_EVERY == 0:
                conn.commit()

        marked += 1

    if args.execute:
        conn.commit()

    if len(ids) > 10:
        print(f"  ... and {len(ids) - 10} more")

    conn.close()
    print(f"\n{'Marked GHOST' if args.execute else 'Would mark GHOST'}: {marked}/{len(ids)}")
    print(f"Skipped: {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
