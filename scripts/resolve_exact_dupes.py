#!/usr/bin/env python3
"""
MUSAEUS — Exact Duplicate Auto-Resolver
For EXACT duplicate groups (confidence 1.0, same audio), picks the best
filename per group using heuristics:
  1. File must exist on disk (gone = auto-archive)
  2. Longer filename = more descriptive = keeper
  3. Contains artist name (has " - " separator) = keeper
  4. Not truncated (no cut-off ending) = keeper
  5. Lives in structured path (Music/Artist/Album/) over flat (m4a/)

Archives the losers and resolves the groups.

Usage:
  python scripts/resolve_exact_dupes.py          # dry run
  python scripts/resolve_exact_dupes.py --apply  # commit to DB
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path

# Use Musaeus config system for DB path instead of hardcoding
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from musaeus.config import get_config
    DB_PATH = get_config().db_path
except (ImportError, ValueError):
    # Fallback if config not loadable
    DB_PATH = Path(
        os.environ.get("MUSAEUS_DB_PATH", "/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/musaeus.db")
    )


def score_filename(path: str) -> int:
    """Higher score = better filename. Returns -1 if file gone from disk."""
    # File gone = immediate disqualification
    if not os.path.isfile(path):
        return -1

    name = Path(path).stem
    score = 0

    # Prefer longer names (more descriptive)
    score += len(name) * 2

    # Prefer names with artist separator " - "
    if " - " in name:
        score += 100

    # Prefer names in structured paths (Music/Artist/Album/) over flat (m4a/)
    if "/Music/" in path:
        score += 200
    elif "/INBOX/Music/" in path:
        score += 150

    # Penalize flat m4a dump paths
    if "/INBOX/m4a/" in path:
        score -= 50

    # Prefer names that don't look truncated
    if not name.endswith(("(", "[", "...", ".")):
        score += 50

    # Penalize very short names (< 15 chars) — likely truncated
    if len(name) < 15:
        score -= 100

    # Penalize "Various Artists" prefix
    if name.lower().startswith("various artists"):
        score -= 200

    # Penalize double-artist pattern (e.g. "Artist - Artist Title")
    parts = name.split(" - ", 1)
    if len(parts) == 2:
        artist_part = parts[0].strip().lower()
        title_part = parts[1].strip().lower()
        if title_part.startswith(artist_part):
            score -= 50  # double-artist name is bad

    return score


def main():
    dry_run = "--apply" not in sys.argv

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Get all pending groups
    groups = conn.execute("""
        SELECT group_id, COUNT(*) as cnt
        FROM duplicates
        WHERE status = 'pending'
        GROUP BY group_id
        ORDER BY group_id
    """).fetchall()

    print(f"MUSAEUS Exact Duplicate Auto-Resolver")
    print(f"  DB: {DB_PATH}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'APPLY'}")
    print(f"  Found {len(groups)} pending groups to resolve\n")

    keep_count = 0
    archive_count = 0
    skip_count = 0
    gone_count = 0

    for group in groups:
        gid = group["group_id"]
        members = conn.execute(
            "SELECT id, file_path, duplicate_type, confidence FROM duplicates WHERE group_id = ? AND status = 'pending'",
            (gid,),
        ).fetchall()

        if len(members) < 2:
            # Single member — stale entry, just resolve it
            if not dry_run:
                conn.execute("UPDATE duplicates SET status = 'resolved' WHERE group_id = ? AND status = 'pending'", (gid,))
            skip_count += 1
            continue

        # Score each member
        scored = [(m, score_filename(m["file_path"])) for m in members]

        # If ALL files are gone from disk, resolve the whole group
        if all(s == -1 for _, s in scored):
            if not dry_run:
                conn.execute("UPDATE duplicates SET status = 'resolved' WHERE group_id = ? AND status = 'pending'", (gid,))
            gone_count += len(members)
            print(f"  {gid}: ALL FILES GONE — resolved")
            continue

        # If some files are gone, archive those and continue scoring survivors
        gone_members = [(m, s) for m, s in scored if s == -1]
        live_members = [(m, s) for m, s in scored if s != -1]

        for m, _ in gone_members:
            if not dry_run:
                conn.execute("UPDATE duplicates SET status = 'archive' WHERE id = ?", (m["id"],))
            gone_count += 1
            print(f"  {gid}: GONE → {Path(m['file_path']).name}")

        if len(live_members) < 2:
            # Only one left alive — it wins
            if live_members:
                winner = live_members[0][0]
                if not dry_run:
                    conn.execute("UPDATE duplicates SET status = 'keep' WHERE id = ?", (winner["id"],))
                keep_count += 1
                print(f"  {gid}: KEEP (sole survivor) → {Path(winner['file_path']).name}")
            continue

        # Pick best among living members
        live_members.sort(key=lambda x: -x[1])
        best = live_members[0][0]
        losers = [m for m, _ in live_members[1:]]

        print(f"  {gid}: KEEP → {Path(best['file_path']).name}")
        for loser in losers:
            print(f"       ARCHIVE → {Path(loser['file_path']).name}")

        if not dry_run:
            conn.execute("UPDATE duplicates SET status = 'keep' WHERE id = ?", (best["id"],))
            keep_count += 1
            for loser in losers:
                conn.execute("UPDATE duplicates SET status = 'archive' WHERE id = ?", (loser["id"],))
                archive_count += 1

    if not dry_run:
        conn.commit()

    print(f"\n{'=== Applied ===' if not dry_run else '=== Dry Run (pass --apply to commit) ==='}")
    print(f"  Kept:       {keep_count}")
    print(f"  Archived:   {archive_count}")
    print(f"  Gone/swept: {gone_count}")
    print(f"  Skipped:    {skip_count}")

    # Final status
    remaining = conn.execute("SELECT COUNT(*) FROM duplicates WHERE status = 'pending'").fetchone()[0]
    print(f"\n  Remaining pending: {remaining}")

    conn.close()


if __name__ == "__main__":
    main()
