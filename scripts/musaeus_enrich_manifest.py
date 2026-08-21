#!/usr/bin/env python3
"""
MUSAEUS — Backfill Manifest Enrichment

DupeResolverStage now writes moved_codec/moved_bitrate/kept_path/
kept_codec/kept_bitrate directly into every new manifest CSV going
forward. This script backfills that same enrichment for a manifest
written BEFORE that fix (source, destination, group_id, duplicate_type
only) by querying the DB read-only -- it never writes to the vault.

For the MOVED file: looked up directly via the manifest's own
`destination` column (archive.file_path there is accurate and stable,
since a DUPE_REVIEW row is never touched again after the move).

For the KEPT file: the original DUPE_MOVED_FOR_REVIEW event only
recorded the keeper's PRE-FINALIZE path (note field, "kept=<path>"),
which goes stale the moment that file is later finalized normally like
any other CATALOGUED file. This script resolves the keeper's CURRENT
path by following the FINALIZE_MOVE event chain (old_value -> new_value)
from that stale path. If the chain doesn't resolve (e.g. the keeper
hasn't been finalized yet, or was itself later moved again by something
else), the kept_* columns are written as "unknown" rather than guessed.

Usage:
    python3 scripts/musaeus_enrich_manifest.py --manifest <old.csv> --db <musaeus.db> [--out <new.csv>]
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path


def _resolve_current_path(conn: sqlite3.Connection, stale_path: str) -> str | None:
    """
    Follow FINALIZE_MOVE and CANONICALIZE event chains forward from a
    possibly-stale path to find where the file actually lives now.
    FINALIZE_MOVE records its new path in `new_value`; CANONICALIZE
    records it in `file_path` (its own `new_value` is the outcome
    string -- PASSTHROUGH/CONVERTED/TRANSCODED -- not a path, so using
    the wrong column here would silently corrupt the chain).
    Returns None if the chain doesn't resolve to anywhere.
    """
    seen = {stale_path}
    current = stale_path
    for _ in range(10):  # bounded: a real chain is 1-2 hops, never unbounded
        row = conn.execute(
            """
            SELECT event_type, file_path, new_value FROM events
             WHERE old_value = ? AND event_type IN ('FINALIZE_MOVE', 'CANONICALIZE')
             ORDER BY id DESC LIMIT 1
            """,
            (current,),
        ).fetchone()
        if not row:
            break
        nxt = row["new_value"] if row["event_type"] == "FINALIZE_MOVE" else row["file_path"]
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        current = nxt
    return current if current != stale_path or _file_row_exists(conn, current) else None


def _file_row_exists(conn: sqlite3.Connection, path: str) -> bool:
    return conn.execute("SELECT 1 FROM archive WHERE file_path = ?", (path,)).fetchone() is not None


def _lookup_codec_bitrate(conn: sqlite3.Connection, path: str) -> tuple[str, str]:
    row = conn.execute(
        "SELECT codec, bitrate FROM archive WHERE file_path = ?", (path,)
    ).fetchone()
    if not row:
        return "unknown", "unknown"
    return (row["codec"] or "unknown"), (str(row["bitrate"]) if row["bitrate"] else "unknown")


def _kept_stale_path_for(conn: sqlite3.Connection, destination: str) -> str | None:
    row = conn.execute(
        """
        SELECT note FROM events
         WHERE event_type = 'DUPE_MOVED_FOR_REVIEW' AND file_path = ?
         ORDER BY id DESC LIMIT 1
        """,
        (destination,),
    ).fetchone()
    if not row or not row["note"]:
        return None
    note = row["note"]
    marker = "kept="
    idx = note.find(marker)
    if idx == -1:
        return None
    kept = note[idx + len(marker) :].strip()
    if kept.startswith("(") :  # "(already in ALAC-Library, prior batch)" / "(no keeper on record)"
        return None
    return kept


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill codec/bitrate enrichment onto an old manifest CSV")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--out", type=Path, help="Output path (default: <manifest>_enriched.csv)")
    args = parser.parse_args()

    if not args.manifest.exists():
        print(f"ERROR: manifest not found: {args.manifest}", file=sys.stderr)
        return 1
    if not args.db.exists():
        print(f"ERROR: db not found: {args.db}", file=sys.stderr)
        return 1

    out_path = args.out or args.manifest.with_name(args.manifest.stem + "_enriched.csv")

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    with open(args.manifest, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    unresolved_kept = 0
    fieldnames = [
        "source", "destination", "group_id", "duplicate_type",
        "moved_codec", "moved_bitrate", "kept_path", "kept_codec", "kept_bitrate",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as out_fh:
        writer = csv.DictWriter(out_fh, fieldnames=fieldnames)
        writer.writeheader()

        for row in rows:
            moved_codec, moved_bitrate = _lookup_codec_bitrate(conn, row["destination"])

            stale_kept = _kept_stale_path_for(conn, row["destination"])
            kept_path = kept_codec = kept_bitrate = "unknown"
            if stale_kept:
                resolved = _resolve_current_path(conn, stale_kept)
                if resolved:
                    kept_path = resolved
                    kept_codec, kept_bitrate = _lookup_codec_bitrate(conn, resolved)
                else:
                    kept_path = stale_kept + "  [STALE -- could not resolve current location]"
                    unresolved_kept += 1
            else:
                unresolved_kept += 1

            writer.writerow({
                "source": row["source"],
                "destination": row["destination"],
                "group_id": row["group_id"],
                "duplicate_type": row["duplicate_type"],
                "moved_codec": moved_codec,
                "moved_bitrate": moved_bitrate,
                "kept_path": kept_path,
                "kept_codec": kept_codec,
                "kept_bitrate": kept_bitrate,
            })

    conn.close()
    print(f"Wrote {len(rows)} row(s) to {out_path}")
    if unresolved_kept:
        print(f"  ⚠ {unresolved_kept} row(s) had an unresolvable kept-file path")
    return 0


if __name__ == "__main__":
    sys.exit(main())
