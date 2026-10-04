#!/usr/bin/env python3
"""Clear the album of every catalogued song whose album is a playlist's name.

Grey, 2026-10-03: "all references to playlist, My Playlist, or my playlist
with any letter [should] be removed ... also for tracks inside ALAC-Archival".
The rule is musaeus/playlist_album.py. New arrivals are handled in Act 1
(Scholar); this clears what is already in the catalogue.

It changes the CATALOGUE only. The next Act 3 does the rest, through the
stages that already own each job:

    tagger    removes the leftover album tag from the master (the catalogue
              has no album, the file has a playlist name)
    organize  moves the song out of its "My playlist S" folder, into "Unsorted"
    edition-build  moves and re-tags every edition copy after its master

A later `musaeus run --act enrichment` tries to fill the albums in, unless a
source has already declined (album_fill_checked_at).

    python3 scripts/clear_playlist_albums.py            # dry run
    python3 scripts/clear_playlist_albums.py --execute

Run it when no musaeus process is running: the next Act 3 moves masters.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from musaeus.config import MusicConfig  # noqa: E402
from musaeus.playlist_album import is_playlist_album  # noqa: E402


def playlist_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT id, file_path, artist, title, album FROM archive "
        "WHERE status='CATALOGUED' AND album IS NOT NULL AND TRIM(album) <> '' ORDER BY artist, title"
    ).fetchall()
    return [r for r in rows if is_playlist_album(r["album"])]


def clear(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> int:
    run_id = f"clear_playlist_albums_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        conn.execute("UPDATE archive SET album=NULL WHERE id=? AND album=?", (r["id"], r["album"]))
        conn.execute(
            "INSERT INTO events (run_id, ts, event_type, file_path, old_value, new_value, stage, note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, ts, "ALBUM_CLEARED", r["file_path"], r["album"], None, "clear-playlist-albums",
             "a playlist's name is not an album (Grey 2026-10-03)"),
        )  # fmt: skip
    conn.commit()
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="clear them (default: dry run)")
    args = ap.parse_args()
    cfg = MusicConfig.from_env()
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000")
    rows = playlist_rows(conn)
    names = sorted({r["album"] for r in rows})
    print(f"{len(rows):,} catalogued song(s) have a playlist name as their album ({len(names)} name(s)):")
    for n in names[:40]:
        print(f"   {n}  x{sum(1 for r in rows if r['album'] == n)}")
    if not args.execute:
        print("\nDRY RUN: nothing changed. Pass --execute to clear them.")
        return 0
    print(f"\nCLEARED {clear(conn, rows):,}. Next: musaeus run --act 3 (tagger removes the tags, organize refiles).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
