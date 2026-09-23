#!/usr/bin/env python3
"""Delete tracks a review marked for removal -- every copy, permanently.

Grey's rule, stated more than once: "delete means delete, not quarantine",
and "delete all copies". This is the tool that does it the same way twice,
because doing it by hand is how a deletion half-happens.

WHAT "PERMANENTLY" REQUIRES

  every tier      A track exists in ALAC-Archival (the master), ALAC_Library
                  (the -18 bake) and CAR_Library (the AAC edition). The row
                  knows two of those paths; the master is found by relative
                  path, the same rule editions.master_path_for uses. Missing
                  the master is how a deleted track returns on the next bake.

  denied_hashes   A deletion that does not reach the deny list is not
                  permanent: the next ingest of the same audio walks straight
                  back in. The list lives in the LEDGER (_db_backups/
                  hash_index.db), deliberately -- the catalogue can be
                  rebuilt, and a deny list wiped by a rebuild would let every
                  purged track back in.

  the row         Removed, with one DELETED_BY_REVIEW event per file so the
                  event log says what happened and to what.

ORDER MATTERS. Files first, then the database, per file. A crash leaves a row
whose files are gone -- visible and re-runnable -- rather than a catalogue
that has forgotten tracks still on disk.

    python3 scripts/delete_reviewed_tracks.py ids.txt --reason "..."
    python3 scripts/delete_reviewed_tracks.py ids.txt --reason "..." --execute
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
from musaeus.db import deny_hash  # noqa: E402

TIERS = ("ALAC-Archival", "ALAC_Library", "CAR_Library")


def copies(row: sqlite3.Row, libs: Path) -> list[Path]:
    """Every path this track could occupy, de-duplicated, order stable."""
    out: list[Path] = []
    seen: set[str] = set()

    def add(p: Path | None) -> None:
        if p and str(p) not in seen:
            out.append(p)
            seen.add(str(p))

    # set(row.keys()), not `in row`: sqlite3.Row's __contains__ tests VALUES,
    # so `"car_export_path" in row` asks whether some column holds that string
    # and is always False. ruff's SIM118 suggests exactly that rewrite here
    # and it would be a silent bug.
    cols = set(row.keys())
    fp = Path(row["file_path"])
    add(fp)
    car = row["car_export_path"] if "car_export_path" in cols else None
    add(Path(car) if car else None)
    try:
        rel = fp.relative_to(libs / "ALAC_Library")
    except ValueError:
        return out
    for tier in TIERS:
        add(libs / tier / rel)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", type=Path, help="file of archive ids, one per line")
    ap.add_argument("--reason", required=True, help="recorded on every event and deny entry")
    ap.add_argument("--execute", action="store_true", help="actually delete (default: dry run)")
    args = ap.parse_args()

    ids = [int(x) for x in args.ids.read_text().split() if x.strip().isdigit()]
    if not ids:
        print(f"no ids in {args.ids}")
        return 1

    cfg = MusicConfig.from_env()
    libs = Path(cfg.vault_root) / "Libraries"
    ledger = Path(cfg.vault_root) / "_db_backups" / "hash_index.db"

    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000")
    led = sqlite3.connect(ledger) if ledger.exists() else None
    if led is None:
        print(f"WARNING: no ledger at {ledger} -- nothing will be denied, so a "
              f"re-ingest of this audio would be accepted again.")

    # Which paths do SURVIVING rows still point at?
    #
    # Two archive rows can share one published file: a track and its "My
    # playlist X" duplicate both carry the same car_export_path, because the
    # car edition is keyed on (artist, title) and they are the same recording.
    # Deleting the duplicate then removes the file the KEPT row names, and the
    # kept row silently becomes a phantom. Measured on this very run before
    # the guard existed: 2 of 82 deletions did exactly that to Paul
    # McCartney's "With a Little Luck" and Rage Against the Machine's "Killing
    # in the Name".
    #
    # A deletion is allowed to remove the track it was asked to remove. It is
    # not allowed to take a different track's file with it.
    doomed = set(ids)
    keep_paths: set[str] = set()
    for col in ("file_path", "car_export_path"):
        try:
            for rid, val in conn.execute(f"SELECT id, {col} FROM archive WHERE COALESCE({col},'') <> ''"):
                if rid not in doomed and val:
                    keep_paths.add(str(Path(val)))
        except sqlite3.Error:
            continue

    run_id = f"delete_reviewed_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:6]}"
    n_files = n_rows = n_denied = n_absent = n_shared = 0

    for i in ids:
        row = conn.execute("SELECT * FROM archive WHERE id=?", (i,)).fetchone()
        if row is None:
            n_absent += 1
            continue
        print(f"\n  id={i}  {row['artist']} - {row['title']}")
        for p in copies(row, libs):
            if not p.is_file():
                continue
            if str(p) in keep_paths:
                print(f"     KEPT (another row still points at it): {p}")
                n_shared += 1
                continue
            print(f"     {'DELETE ' if args.execute else 'would delete '}{p}")
            if args.execute:
                p.unlink()
                conn.execute(
                    "INSERT INTO events (run_id, ts, event_type, file_path, old_value, "
                    "new_value, note) VALUES (?,?,?,?,?,?,?)",
                    (run_id, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                     "DELETED_BY_REVIEW", str(p), str(p), "", args.reason))
            n_files += 1
        h = row["audio_hash"] if "audio_hash" in set(row.keys()) else None
        if h and led is not None:
            if args.execute:
                deny_hash(led, h, args.reason, row["file_path"])
            n_denied += 1
        if args.execute:
            conn.execute("DELETE FROM archive WHERE id=?", (i,))
        n_rows += 1

    if args.execute:
        conn.commit()
        if led is not None:
            led.commit()
    conn.close()
    if led is not None:
        led.close()

    print(f"\n{'DELETED' if args.execute else 'DRY RUN'}: {n_files} file(s), "
          f"{n_rows} row(s), {n_denied} hash(es) denied"
          + (f", {n_absent} id(s) already gone" if n_absent else "")
          + (f", {n_shared} file(s) kept because a surviving row needs them" if n_shared else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
