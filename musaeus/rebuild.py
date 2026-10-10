#!/usr/bin/env python3
"""
MUSAEUS — Rebuild DB from Event Log  [DISABLED — DO NOT USE]

DISABLED 2026-08-21. This module is retained for reference only; the entry
point refuses to run. It was never safe to use and would destroy data.

The original premise -- stated in this docstring and quoted below as it
stood -- was:

    "The event log is the immutable source of truth. This module replays
     all events to reconstruct the `archive` table from scratch."

That is **false**, in two independent ways, both confirmed against the real
250,000-event live database:

1. The event-type names it dispatches on do not exist. It handles
   FILE_REGISTERED / FILE_HASHED / FILE_CATALOGUED / STATUS_CHANGE /
   FIELD_UPDATE / LUFS_MEASURED / RG_TAGGED / CAR_EXPORTED / FILE_GHOST /
   FILE_REMOVED. The live DB contains INGEST / HASH_COMPUTED /
   METADATA_EXTRACTED / FINALIZE_MOVE / FORGE_TAG / BPM_ANALYZED / ... --
   34 real types, with **zero overlap**. Every replay branch is dead code.

2. Far worse, the events are a human-readable audit trail, not an
   event-sourcing store, and they are lossy by design:
     - HASH_COMPUTED stores "0faef0355d05cb91…" -- 16 characters and a
       literal ellipsis. A real sha256 is 64. audio_hash/full_hash are
       physically unrecoverable from the log.
     - METADATA_EXTRACTED stores only artist/title/bitrate. album, genre,
       year, track, duration, sample_rate, channels and codec are never
       recorded at all.
     - FORGE_TAG records lufs and rg_gain but not lufs_tp or rg_peak;
       BPM_ANALYZED records bpm and key but not energy or danceability.

So even with the names corrected, the archive table could not be
reconstructed -- the data simply is not in the log.

The failure mode was the dangerous part: rebuild_archive_from_events()
issues `DELETE FROM archive` *first*, then replays. Run against the real
vault it would have wiped ~27,000 rows and repopulated metadata-less
status='PENDING' stubs, silently, while reporting success.

The real recovery paths, both proven in practice:
  - the pre-reset DB snapshot written to ALAC-Library/_history/ (this is
    what actually recovered the vault on 2026-08-20), and
  - rebuilding from disk + embedded file tags, which is where the real
    metadata lives.

Usage:  refuses to run; see RebuildDisabledError below.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


class RebuildDisabledError(RuntimeError):
    """Raised on any attempt to run the event-log rebuild.

    Fail closed rather than fail silently: the old behaviour deleted the
    archive table before discovering it could not rebuild it, and reported
    success afterwards. See this module's docstring for the evidence.
    """


_DISABLED_MESSAGE = (
    "rebuild-db is DISABLED and will not run.\n"
    "\n"
    "It cannot do what its name claims. Verified against the real "
    "250,000-event database:\n"
    "  * none of the event-type names it handles exist any more (zero "
    "overlap with the 34 real types)\n"
    "  * the event log is lossy by design -- hashes are stored truncated "
    "to 16 chars + an ellipsis\n"
    "    (a real sha256 is 64), and album/genre/year/track/duration/codec "
    "are never recorded at all\n"
    "\n"
    "It also ran DELETE FROM archive *before* replaying, so it would have "
    "destroyed ~27,000 rows\n"
    "and replaced them with empty PENDING stubs, while reporting success.\n"
    "\n"
    "Use instead:\n"
    "  * the pre-reset snapshot in ALAC-Library/_history/ (this is what "
    "actually recovered the\n"
    "    vault on 2026-08-20), or\n"
    "  * a rebuild from disk + embedded file tags, where the real metadata "
    "actually lives."
)


def rebuild_archive_from_events(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict:
    """DISABLED — always raises RebuildDisabledError. See module docstring.

    The original implementation is preserved below the raise for reference,
    deliberately unreachable rather than deleted, so the shape of what was
    attempted stays legible next to the reasons it could not work.
    """
    raise RebuildDisabledError(_DISABLED_MESSAGE)
    summary: dict[str, Any] = {
        "cleared": 0,
        "replayed": 0,
        "files_rebuilt": 0,
        "errors": [],
    }

    # Count existing archive rows before clearing
    existing = conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
    summary["cleared"] = existing

    if dry_run:
        # In dry-run, just count what we'd replay
        event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        unique_files = conn.execute(
            "SELECT COUNT(DISTINCT file_path) FROM events WHERE file_path IS NOT NULL"
        ).fetchone()[0]
        summary["replayed"] = event_count
        summary["files_rebuilt"] = unique_files
        return summary

    # ── Step 1: Clear the archive table ──────────────────────────────────────
    conn.execute("DELETE FROM archive")

    # ── Step 2: Replay events in chronological order ─────────────────────────
    events = conn.execute("SELECT * FROM events ORDER BY id ASC").fetchall()
    summary["replayed"] = len(events)

    # Track per-file state as we replay
    file_state: dict[str, dict] = {}

    for event in events:
        event_type = event["event_type"]
        file_path = event["file_path"]

        # Skip non-file events (RUN_START, RUN_END, etc.)
        if not file_path:
            continue

        # Initialize file entry if first time seeing it
        if file_path not in file_state:
            file_state[file_path] = {
                "file_path": file_path,
                "filename": Path(file_path).name if file_path else None,
                "ext": Path(file_path).suffix.lower() if file_path else None,
                "status": "PENDING",
            }

        state = file_state[file_path]

        # Apply event effects
        if event_type == "FILE_REGISTERED":
            state["status"] = "PENDING"
            state["date_added"] = event["ts"]
            state["last_seen"] = event["ts"]
            # new_value may contain JSON with initial metadata
            if event["new_value"]:
                try:
                    meta = json.loads(event["new_value"])
                    state.update(meta)
                except (json.JSONDecodeError, TypeError):
                    pass

        elif event_type == "FILE_HASHED":
            state["status"] = "HASHED"
            if event["new_value"]:
                try:
                    data = json.loads(event["new_value"])
                    if "audio_hash" in data:
                        state["audio_hash"] = data["audio_hash"]
                    if "full_hash" in data:
                        state["full_hash"] = data["full_hash"]
                    if "size_bytes" in data:
                        state["size_bytes"] = data["size_bytes"]
                except (json.JSONDecodeError, TypeError):
                    # Might be a plain hash string
                    state["audio_hash"] = event["new_value"]

        elif event_type == "FILE_CATALOGUED":
            state["status"] = "CATALOGUED"
            if event["new_value"]:
                try:
                    meta = json.loads(event["new_value"])
                    for key in (
                        "artist",
                        "album",
                        "title",
                        "genre",
                        "year",
                        "track",
                        "duration",
                        "bitrate",
                        "sample_rate",
                        "channels",
                        "codec",
                    ):
                        if key in meta:
                            state[key] = meta[key]
                except (json.JSONDecodeError, TypeError):
                    pass

        elif event_type == "STATUS_CHANGE":
            if event["new_value"]:
                state["status"] = event["new_value"]

        elif event_type == "FIELD_UPDATE":
            # Generic field update: note contains field name, new_value has the value
            field_name = event["note"]
            if field_name and field_name in state:
                state[field_name] = event["new_value"]

        elif event_type == "LUFS_MEASURED":
            if event["new_value"]:
                try:
                    data = json.loads(event["new_value"])
                    state.update(
                        {k: data[k] for k in ("lufs", "lufs_tp", "rg_gain", "rg_peak") if k in data}
                    )
                except (json.JSONDecodeError, TypeError):
                    pass

        elif event_type == "RG_TAGGED":
            state["rg_tagged_at"] = event["ts"]

        elif event_type == "CAR_EXPORTED":
            state["car_export_path"] = event["new_value"]
            if event["note"]:
                state["noise_profile"] = event["note"]

        elif event_type == "FILE_GHOST":
            state["status"] = "GHOST"

        elif event_type == "FILE_REMOVED":
            # File explicitly removed — don't include in rebuilt archive
            del file_state[file_path]
            continue

        # Update last_seen for any event touching this file
        state["last_seen"] = event["ts"]

    # ── Step 3: Write rebuilt archive ────────────────────────────────────────
    archive_fields = [
        "file_path",
        "audio_hash",
        "full_hash",
        "filename",
        "ext",
        "size_bytes",
        "artist",
        "album",
        "title",
        "genre",
        "year",
        "track",
        "duration",
        "bitrate",
        "sample_rate",
        "channels",
        "codec",
        "status",
        "date_added",
        "last_seen",
        "last_modified",
        "lufs",
        "lufs_tp",
        "rg_gain",
        "rg_peak",
        "rg_tagged_at",
        "car_export_path",
        "noise_profile",
    ]

    for file_path, state in file_state.items():
        values = [state.get(f) for f in archive_fields]
        placeholders = ", ".join("?" for _ in archive_fields)
        try:
            conn.execute(
                f"INSERT INTO archive ({', '.join(archive_fields)}) VALUES ({placeholders})",
                values,
            )
        except sqlite3.Error as exc:
            summary["errors"].append(f"{file_path}: {exc}")

    conn.commit()
    summary["files_rebuilt"] = len(file_state)
    return summary


def cmd_rebuild_db(dry_run: bool = False) -> int:
    """CLI entry point for rebuild-db — DISABLED, prints why and exits 2.

    Deliberately refuses even with --dry-run: there is nothing safe to
    preview, and printing a plausible-looking plan would imply the real
    run works. Exit code 2 matches the P0-02 dry-run guard's
    "refused, did not run" convention rather than 1 ("ran and failed").
    """
    print(f"\nMusaeus rebuild-db\n\n{_DISABLED_MESSAGE}\n", file=sys.stderr)
    return 2

    # ── unreachable: original implementation kept for reference ──────────
    from .config import get_config  # type: ignore[unreachable]
    from .db import open_db

    try:
        cfg = get_config()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    conn = open_db(cfg.db_path)
    try:
        mode = " [DRY RUN]" if dry_run else ""
        print(f"\nMusaeus rebuild-db{mode}")
        print(f"  DB: {cfg.db_path}")
        print()

        summary = rebuild_archive_from_events(conn, dry_run=dry_run)

        if dry_run:
            print(f"  Would clear  : {summary['cleared']:,} archive rows")
            print(f"  Would replay : {summary['replayed']:,} events")
            print(f"  Would rebuild: {summary['files_rebuilt']:,} file entries")
        else:
            print(f"  Cleared      : {summary['cleared']:,} archive rows")
            print(f"  Replayed     : {summary['replayed']:,} events")
            print(f"  Rebuilt      : {summary['files_rebuilt']:,} file entries")

        if summary["errors"]:
            print(f"\n  Errors ({len(summary['errors'])}):")
            for err in summary["errors"][:20]:
                print(f"    ✗ {err}")

        print()
        return 0 if not summary["errors"] else 1
    finally:
        conn.close()
