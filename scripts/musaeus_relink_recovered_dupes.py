#!/usr/bin/env python3
"""
MUSAEUS — Relink recovered DUPE_REVIEW rows (2026-08-14 incident)

Context: ALAC-Library/2026-08-12 vanished from disk (cause still
unconfirmed) while archive.db kept 14,467 CATALOGUED rows pointing at
it. The 2026-08-14 overnight run re-ingested a large share of the same
music from its original source; CrossDupe correctly flagged those
fresh imports as "already in ALAC-Library" against the (by-then
phantom) 2026-08-12 records, and DupeResolver correctly moved them to
DUPES_MOVED_FOR_REVIEW -- correct behavior IF the original had still
been there. It wasn't.

A hash cross-reference against ALAC-Library/_history/hash_index.db
(Finalize's own independent ledger of every audio_hash ever finalized,
untouched by this incident) plus an exhaustive full-filesystem
audio_hash scan of every file on /mnt/FORGE2TB confirmed:
  - 10,644 DUPE_REVIEW rows: audio content verified alive on disk,
    misfiled as "duplicate of" a file that no longer exists anywhere.
  - Every one of those 10,644 was individually checked against its own
    DUPE_MOVED_FOR_REVIEW event's kept= reference: in 0/10,644 cases
    does the kept file still exist. None of these are genuine
    duplicates of currently-valid content.
  - 4,608 rows: no matching audio content found anywhere on the
    filesystem. Genuinely gone -- out of scope for this script,
    GhostStage's to mark.

What this script does: for each recovered row, move the file from its
DUPES_MOVED_FOR_REVIEW holding location to where it would have landed
under a normal Finalize (ALAC-Library/<today>/<Artist>/<Album>/<Track>,
same build_track_filename/sanitize_path_component/unique_path
convention Finalize and DupeResolver already use), and flip
archive.status back to CATALOGUED with the new path. duplicates rows
are left untouched -- they're a historical ledger of what DupeResolver
did and why, not something to rewrite.

Safety:
  - Dry run (no --execute) by default -- prints exactly what would
    move where and what the DB update would be, no writes.
  - --limit N caps how many rows are shown/touched, for reviewing a
    sample before running against everything.
  - Every row is re-verified live at run time (status still
    DUPE_REVIEW, file still exists at its current path, target does
    not already exist) -- no reliance on a stale snapshot.
  - unique_path() prevents clobbering anything already at the target.
  - Logs a RELINK_RECOVERED event per row (old_value/new_value) for a
    full audit trail, same shape as DupeResolver's own event logging.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.stages.organize import build_track_filename, sanitize_path_component, unique_path  # noqa: E402

VAULT_ROOT = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT")
DB_PATH = VAULT_ROOT / "musaeus.db"
HASH_INDEX_PATH = VAULT_ROOT / "ALAC-Library" / "_history" / "hash_index.db"
ALAC_LIBRARY = VAULT_ROOT / "ALAC-Library"


def _batch_date() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _recovered_rows(conn: sqlite3.Connection) -> list[dict]:
    conn.execute(f"ATTACH DATABASE '{HASH_INDEX_PATH}' AS h")
    rows = conn.execute(
        """
        SELECT a.file_path, a.audio_hash, a.artist, a.album, a.title, a.ext
          FROM main.archive a
          JOIN h.finalized_hashes h ON h.audio_hash = a.audio_hash
         WHERE a.status = 'DUPE_REVIEW'
         GROUP BY a.file_path
        """
    ).fetchall()
    conn.execute("DETACH DATABASE h")
    return [dict(r) for r in rows]


def _target_path(row: dict, source: Path, batch_date: str) -> Path:
    artist = row.get("artist") or "Unknown Artist"
    album = row.get("album") or "Unsorted"
    title = row.get("title") or "Unknown Title"
    filename = build_track_filename(artist, title, source.suffix)
    target_dir = ALAC_LIBRARY / batch_date / sanitize_path_component(artist) / sanitize_path_component(album)
    return unique_path(target_dir / filename)


def main() -> int:
    parser = argparse.ArgumentParser(description="Relink recovered DUPE_REVIEW rows back into ALAC-Library")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N matching rows")
    parser.add_argument("--execute", action="store_true", help="Actually move files + update DB (default: dry run)")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = _recovered_rows(conn)
    if args.limit:
        rows = rows[: args.limit]

    batch_date = _batch_date()
    mode = "EXECUTING" if args.execute else "DRY RUN (pass --execute to actually perform this)"
    print(f"{mode} — {len(rows)} row(s) to process, target batch date {batch_date}\n")

    moved = 0
    skipped = 0
    for row in rows:
        source = Path(row["file_path"])

        # Live re-check -- never trust the snapshot taken above.
        current = conn.execute(
            "SELECT status FROM archive WHERE file_path = ?", (str(source),)
        ).fetchone()
        if not current or current["status"] != "DUPE_REVIEW":
            print(f"  SKIP  {source.name}: no longer DUPE_REVIEW (already handled)")
            skipped += 1
            continue
        if not source.exists():
            print(f"  SKIP  {source.name}: file no longer exists at recorded path")
            skipped += 1
            continue

        target = _target_path(row, source, batch_date)
        if target.exists():
            print(f"  SKIP  {source.name}: target already exists ({target})")
            skipped += 1
            continue

        print(f"  [{row.get('artist')} — {row.get('title')}]")
        print(f"    from: {source}")
        print(f"    to:   {target}")
        print("    DB:   archive.status DUPE_REVIEW -> CATALOGUED, file_path -> new path")

        if not args.execute:
            moved += 1
            continue

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
        except OSError as exc:
            print(f"    FAILED: {exc}", file=sys.stderr)
            skipped += 1
            continue

        conn.execute(
            "UPDATE archive SET status = 'CATALOGUED', file_path = ? WHERE file_path = ?",
            (str(target), str(source)),
        )
        conn.execute(
            "INSERT INTO events (run_id, event_type, file_path, old_value, new_value, stage, note) "
            "VALUES (?, 'RELINK_RECOVERED', ?, ?, ?, 'relink-recovered-dupes', ?)",
            (
                f"relink_{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
                str(target),
                str(source),
                str(target),
                f"audio_hash={row['audio_hash']} recovered from phantom-duplicate misclassification",
            ),
        )
        conn.commit()
        moved += 1
        print("    done")

    print(f"\n{moved} {'relinked' if args.execute else 'would relink'}, {skipped} skipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
