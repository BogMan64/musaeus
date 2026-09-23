#!/usr/bin/env python3
"""Lift the deny-list entry for tracks a review decided to keep.

The opposite of delete_reviewed_tracks.py, and built the same way: a file of
archive ids, a --reason, and a dry run unless --execute is given.

WHY IT EXISTS. On 2026-09-23 a rebuild made with --skip deny-list had
re-admitted 1,359 tracks Grey had ruled out. He reviewed them and kept 170.
Those 170 still carried denied audio, so doctor failed on every one and the
next normal rebuild would have refused them.

WHAT IT RECORDS. The ledger keeps no history, so each lift writes an
UNDENIED_BY_REVIEW event carrying the ruling it lifted -- reason and date --
into the event log. Without that, the reason the audio had been denied would
simply be gone.

    python3 scripts/undeny_hashes.py ids.txt --reason "..."
    python3 scripts/undeny_hashes.py ids.txt --reason "..." --execute
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from musaeus.config import MusicConfig  # noqa: E402
from musaeus.db import undeny_hash  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", type=Path, help="file of archive ids, one per line")
    ap.add_argument("--reason", required=True, help="why the ruling is being reversed; recorded on every event")
    ap.add_argument("--execute", action="store_true", help="actually lift (default: dry run)")
    args = ap.parse_args()

    ids = [int(x) for x in args.ids.read_text().split() if x.strip().isdigit()]
    if not ids:
        print(f"no ids in {args.ids}")
        return 1
    cfg = MusicConfig.from_env()
    ledger = Path(cfg.vault_root) / "_db_backups" / "hash_index.db"
    if not ledger.exists():
        print(f"no ledger at {ledger} -- nothing is denied, so nothing to lift")
        return 1
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000")
    led = sqlite3.connect(ledger)
    led.execute("PRAGMA busy_timeout = 60000")

    run_id = f"undeny_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:6]}"
    n_lifted = n_not_denied = n_absent = 0
    for i in ids:
        row = conn.execute("SELECT * FROM archive WHERE id=?", (i,)).fetchone()
        if row is None or not row["audio_hash"]:
            n_absent += 1
            continue
        h = row["audio_hash"]
        denied = led.execute("SELECT reason, denied_at FROM denied_hashes WHERE audio_hash=?", (h,)).fetchone()
        label = f"id={i}  {row['artist']} - {row['title']}"
        if denied is None:
            print(f"  {label}: not on the deny list -- nothing to lift")
            n_not_denied += 1
            continue
        print(f"  {label}: {'LIFT' if args.execute else 'would lift'}  [was: {denied[0]}]")
        if args.execute:
            lifted = undeny_hash(led, h)
            was = f"{lifted['reason']} ({lifted['denied_at']})" if lifted is not None else "?"
            conn.execute(
                "INSERT INTO events (run_id, ts, event_type, file_path, old_value, new_value, note) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "UNDENIED_BY_REVIEW",
                 row["file_path"], h, "", f"{args.reason} [was denied: {was}]"),
            )
        n_lifted += 1
    if args.execute:
        led.commit()
        conn.commit()
    led.close()
    conn.close()
    print(f"\n{'LIFTED' if args.execute else 'DRY RUN'}: {n_lifted} deny entr{'y' if n_lifted == 1 else 'ies'}"
          + (f", {n_not_denied} track(s) not on the deny list" if n_not_denied else "")
          + (f", {n_absent} id(s) with no catalogue row or no fingerprint" if n_absent else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
