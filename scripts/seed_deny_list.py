#!/usr/bin/env python3
"""
Seed the deny-list from content that was deliberately removed.

The finalized-hash ledger already records every file ever finalized into
ALAC-Library. An entry whose path no longer exists, and whose audio_hash
is not held anywhere in the library, is content that was removed — the
purges of 2026-08-21..24: knock-offs, non-music, taste calls, and the two
lossy duplicates.

That set is exactly what the deny-list wants. Note what is deliberately
excluded: an entry whose hash IS still held is a file that was merely
renamed or moved, and denying it would quarantine the owner's own music
on the next ingest.

Reads the vault DB read-only. Writes only to hash_index.db, and only
inserts.

Usage:
    python3 scripts/seed_deny_list.py                 # dry run
    python3 scripts/seed_deny_list.py --apply
    python3 scripts/seed_deny_list.py --list
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.db import deny_hash, ensure_deny_list  # noqa: E402

VAULT = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT")
DB = VAULT / "musaeus.db"
LEDGER = VAULT / "_db_backups" / "hash_index.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    ap.add_argument("--list", action="store_true", help="show the current deny-list and exit")
    args = ap.parse_args()

    if not LEDGER.exists():
        print(f"no ledger at {LEDGER}")
        return 1

    led = sqlite3.connect(LEDGER)
    led.row_factory = sqlite3.Row
    ensure_deny_list(led)

    if args.list:
        rows = led.execute(
            "SELECT audio_hash, reason, source_path, denied_at FROM denied_hashes "
            "ORDER BY denied_at, source_path"
        ).fetchall()
        print(f"deny-list: {len(rows)} recording(s)\n")
        for r in rows:
            print(f"  {r['audio_hash'][:12]}  {Path(r['source_path'] or '?').name[:58]:<60} {r['reason']}")
        led.close()
        return 0

    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    held = {h for (h,) in db.execute(
        "SELECT audio_hash FROM archive WHERE audio_hash IS NOT NULL AND trim(audio_hash) != ''"
    )}
    db.close()

    already = {h for (h,) in led.execute("SELECT audio_hash FROM denied_hashes")}

    removed, moved, seen = [], 0, set()
    for r in led.execute("SELECT audio_hash, file_path FROM finalized_hashes"):
        if Path(r["file_path"]).exists():
            continue
        if r["audio_hash"] in held:
            moved += 1          # renamed/moved: the owner still has this audio
            continue
        if r["audio_hash"] in already or r["audio_hash"] in seen:
            continue
        seen.add(r["audio_hash"])
        removed.append(r)

    print("ledger entries naming a file not on disk, whose audio is:")
    print(f"  still held (renamed/moved) — SKIPPED: {moved}")
    print(f"  gone (removed content)     — to deny: {len(removed)}")
    print(f"already on the deny-list: {len(already)}\n")

    for r in removed[:10]:
        print(f"    {Path(r['file_path']).name[:70]}")
    if len(removed) > 10:
        print(f"    ... and {len(removed) - 10} more")

    if args.apply:
        for r in removed:
            deny_hash(led, r["audio_hash"], "removed from the library by owner decision",
                      r["file_path"])
        led.commit()
        total = led.execute("SELECT COUNT(*) FROM denied_hashes").fetchone()[0]
        print(f"\napplied. deny-list now holds {total} recording(s).")
    else:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
    led.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
