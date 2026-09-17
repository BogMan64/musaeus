#!/usr/bin/env python3
"""Write reviewed album proposals into the catalogue.

The writing half of `propose_album_names.py`, deliberately a separate
program: proposing is safe and repeatable, writing is neither, and one
`--live` flag should not be reachable by accident from a read-only run.

Dry run by default. Nothing is written until `--live`.

What this refuses to do
-----------------------
**It never overwrites an album that is already set.** The version of this
that ran on 2026-09-13 selected on `confidence == "1-AGREED"` and updated
whatever row the CSV's archive_id named, with no check on the current value.
It was safe only because every row in that CSV happened to have a blank
album. Re-run the same CSV after a later pass had filled some of those in,
and it would have quietly replaced real album names with proposals -- the
rows it is least entitled to touch being exactly the ones a human had
already ruled on.

A proposal answers "what should go in this empty field". It is not a
correction, and it does not outrank anything already there. A row whose
album has changed since the CSV was generated is reported and skipped.

Every write is logged to the events table as ALBUM_NAME_APPLIED with the old
value (blank) and the new one, so an apply can be audited or reversed from
the event log rather than from memory.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from musaeus.handoff import write_tool_handoff  # noqa: E402

VAULT_DB = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/musaeus.db")

#: Tiers this will write without further argument. "1-AGREED" means two or
#: more independent sources named the same album; the single-source and
#: disagreement tiers are judgement calls and stay out unless asked for.
DEFAULT_TIERS = ("1-AGREED",)


def _events_table_exists(conn: sqlite3.Connection) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()
    )


def apply_rows(
    conn: sqlite3.Connection,
    rows: list[dict],
    tiers: tuple[str, ...],
    *,
    live: bool,
    run_id: str | None = None,
) -> dict[str, int]:
    """Apply proposals, returning a tally of what happened to each row.

    `run_id` groups this apply's events, matching the convention the rest of
    the vault uses (`lufs_bake_20260914T122238Z` and friends). events.run_id
    is NOT NULL in the real schema; omitting it aborted the first live apply
    on 2026-09-14 with an IntegrityError -- cleanly, nothing written, but the
    unit tests had passed because their fixture table did not carry the
    constraint. A fixture looser than production is a test that agrees with
    you for the wrong reason.
    """
    if run_id is None:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"album_names_{stamp}"
    tally = {
        "considered": 0,
        "written": 0,
        "skipped_album_already_set": 0,
        "skipped_no_proposal": 0,
        "skipped_wrong_tier": 0,
        "row_not_found": 0,
    }
    log_events = _events_table_exists(conn)

    for row in rows:
        if row.get("confidence") not in tiers:
            tally["skipped_wrong_tier"] += 1
            continue
        tally["considered"] += 1

        archive_id = (row.get("archive_id") or "").strip()
        proposed = (row.get("proposed_album") or "").strip()
        if not archive_id or not proposed:
            tally["skipped_no_proposal"] += 1
            continue

        found = conn.execute(
            "SELECT album FROM archive WHERE id = ?", (archive_id,)
        ).fetchone()
        if found is None:
            tally["row_not_found"] += 1
            continue

        current = (found[0] or "").strip()
        if current:
            # The guard. See the module docstring.
            tally["skipped_album_already_set"] += 1
            continue

        if live:
            conn.execute(
                "UPDATE archive SET album = ? WHERE id = ?", (proposed, archive_id)
            )
            if log_events:
                conn.execute(
                    "INSERT INTO events (run_id, event_type, file_path, old_value, new_value, note) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        run_id,
                        "ALBUM_NAME_APPLIED",
                        row.get("file_path", ""),
                        "",
                        proposed,
                        f"{row.get('confidence','')}; {row.get('note','')}".strip("; "),
                    ),
                )
        tally["written"] += 1

    if live:
        conn.commit()
    return tally


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("csv_path", type=Path, help="proposal CSV from propose_album_names.py")
    ap.add_argument("--db", type=Path, default=VAULT_DB)
    ap.add_argument(
        "--tier",
        action="append",
        default=None,
        help=f"confidence tier to apply; repeatable (default: {', '.join(DEFAULT_TIERS)})",
    )
    ap.add_argument("--live", action="store_true", help="actually write (default: dry run)")
    args = ap.parse_args(argv)

    tiers = tuple(args.tier) if args.tier else DEFAULT_TIERS

    if not args.csv_path.is_file():
        print(f"Proposal CSV not found: {args.csv_path}")
        return 2
    if not args.db.is_file():
        print(f"Vault database not found: {args.db}")
        return 2

    with open(args.csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    print(f"proposals   : {args.csv_path}  ({len(rows):,} rows)")
    print(f"applying    : {', '.join(tiers)}")
    print(f"mode        : {'LIVE — writing to the catalogue' if args.live else 'DRY RUN — nothing written'}")
    print()

    conn = sqlite3.connect(args.db)
    try:
        tally = apply_rows(conn, rows, tiers, live=args.live)
    finally:
        conn.close()

    verb = "written" if args.live else "would be written"
    print(f"  {verb:<28} {tally['written']:,}")
    print(f"  in a tier not being applied  {tally['skipped_wrong_tier']:,}")
    if tally["skipped_album_already_set"]:
        print(
            f"  SKIPPED, album already set   {tally['skipped_album_already_set']:,}"
            "   (a proposal never overwrites an existing album)"
        )
    if tally["skipped_no_proposal"]:
        print(f"  no proposal in the row       {tally['skipped_no_proposal']:,}")
    if tally["row_not_found"]:
        print(f"  archive_id not in the DB     {tally['row_not_found']:,}")

    if args.live:
        problems = []
        if tally["skipped_album_already_set"]:
            problems.append(
                f"{tally['skipped_album_already_set']:,} row(s) already had an album and "
                "were left alone. A proposal never overwrites an existing album -- if you "
                "expected these to change, the CSV is older than the catalogue."
            )
        if tally["row_not_found"]:
            problems.append(
                f"{tally['row_not_found']:,} archive_id(s) in the CSV are not in the "
                "database. Those rows were deleted, or the CSV came from another vault."
            )
        hand = write_tool_handoff(
            args.db.parent / "RUNS",
            "album_names_apply",
            summary={
                "proposal CSV": str(args.csv_path),
                "tiers applied": ", ".join(tiers),
                "album names written": tally["written"],
                "skipped, album already set": tally["skipped_album_already_set"],
                "rows in another tier": tally["skipped_wrong_tier"],
            },
            notes=[
                "Every write is logged to the events table as ALBUM_NAME_APPLIED, "
                "grouped under one run_id, so this apply is reversible from the "
                "event log rather than from memory.",
            ],
            problems=problems,
        )
        if hand:
            print(f"\n-> {hand}   (paste this into any AI session)")

    if not args.live:
        print("\nTo write these, re-run with --live:")
        print(f"  python3 {Path(__file__).name} {args.csv_path} --live")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
