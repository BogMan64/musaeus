#!/usr/bin/env python3
"""
MUSAEUS — Mark confirmed-gone phantom rows as GHOST (2026-08-14 incident, phase 2)

Context: ALAC-Library/2026-08-12 vanished from disk while archive.db kept
14,467 CATALOGUED rows pointing at it. Phase 1 (musaeus_relink_recovered_dupes.py)
recovered 10,644 of those via a live copy re-ingested by the 2026-08-14
overnight run and misfiled by DupeResolver as duplicates. This script covers
the remainder that are genuinely unrecoverable.

Scope, derived read-only and confirmed against the live DB before writing
this script: for every audio_hash in ALAC-Library/_history/hash_index.db's
finalized_hashes table (the untouched, pre-incident 2026-08-12 finalize
ledger, 15,084 rows), find every archive row sharing that hash. A hash
qualifies for this script only if EVERY matching archive row is phantom
(file missing on disk) -- i.e. no live copy of that audio content exists
anywhere, not even one recovered by phase 1. That set is exactly 4,608
archive rows, 1:1 with 4,608 distinct hashes, all status='CATALOGUED', all
under the vanished ALAC-Library/2026-08-12 path.

This is NOT the same as running GhostStage's generic full-archive sweep:
that sweep is unscoped and would also catch phantom rows that DO have a
live sibling copy elsewhere (6,845 of them), which must stay CATALOGUED
since their content isn't actually gone -- just filed under a dead path.

What this script does: for each of the 4,608 confirmed-gone rows, set
archive.status = 'GHOST' (GhostStage's own status value -- same meaning,
same downstream exclusion behavior in approval.py/health.py) and log a
GHOST_FOUND event (GhostStage's own event type/shape), scoped to exactly
this id list. No files are moved or deleted -- GHOST is a pure status
label; nothing on disk changes.

Safety:
  - Dry run (no --execute) by default.
  - Every row is re-verified live at run time (status still CATALOGUED,
    file still missing on disk) before being touched -- no reliance on
    the snapshot taken when this script was written.

Usage:
    python3 scripts/musaeus_ghost_confirmed_gone.py            # dry run
    python3 scripts/musaeus_ghost_confirmed_gone.py --execute  # write
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

VAULT_ROOT = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT")
DB_PATH = VAULT_ROOT / "musaeus.db"
HASH_INDEX_PATH = VAULT_ROOT / "ALAC-Library" / "_history" / "hash_index.db"

_COMMIT_EVERY = 500


def _confirmed_gone_ids(conn: sqlite3.Connection) -> list[int]:
    conn.execute(f"ATTACH DATABASE '{HASH_INDEX_PATH}' AS h")
    rows = conn.execute(
        """
        SELECT fh.audio_hash AS hash, a.id AS id, a.file_path AS path
          FROM h.finalized_hashes fh
          JOIN main.archive a ON a.audio_hash = fh.audio_hash
        """
    ).fetchall()
    conn.execute("DETACH DATABASE h")

    from collections import defaultdict

    by_hash: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_hash[r["hash"]].append(r)

    ids: list[int] = []
    for _hash, recs in by_hash.items():
        if not any(Path(r["path"]).exists() for r in recs):
            ids.extend(r["id"] for r in recs)
    return ids


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="Actually write DB changes (default: dry run)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    ids = sorted(_confirmed_gone_ids(conn))
    print(f"{'EXECUTING' if args.execute else 'DRY RUN'} — {len(ids)} confirmed-gone row(s)\n")

    marked = 0
    skipped = 0
    for rowid in ids:
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

        if marked < 10 or args.execute is False:
            print(f"  id={rowid:<6} CATALOGUED -> GHOST   {row['file_path']}")

        if args.execute:
            conn.execute(
                "UPDATE archive SET status='GHOST', last_seen=datetime('now') WHERE id=?",
                (rowid,),
            )
            conn.execute(
                "INSERT INTO events (run_id, event_type, file_path, old_value, new_value, stage, note) "
                "VALUES (?, 'GHOST_FOUND', ?, ?, 'GHOST', 'ghost-confirmed-gone-manual', ?)",
                (
                    f"ghost_{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
                    row["file_path"],
                    row["status"],
                    "confirmed gone: no live audio_hash match anywhere (2026-08-14 incident phase 2)",
                ),
            )
            if marked % _COMMIT_EVERY == 0:
                conn.commit()

        marked += 1

    if args.execute:
        conn.commit()

    if not args.execute and len(ids) > 10:
        print(f"  ... and {len(ids) - 10} more (showing first 10 in dry run)")

    conn.close()
    print(f"\n{'Marked GHOST' if args.execute else 'Would mark GHOST'}: {marked}/{len(ids)}")
    print(f"Skipped: {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
