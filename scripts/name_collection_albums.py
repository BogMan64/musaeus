#!/usr/bin/env python3
"""Give an artist's albumless songs a collection album, where there are two or more.

Grey, 2026-10-04: after every web source has had its say, an artist with TWO OR
MORE songs and no album gets one -- "Louis Armstrong Collection". Classical gets
"<Composer>: Collected Works", and The Commitments (two soundtrack CDs) get
"The Commitments Soundtrack". A single song stays as it is: a one-song "album"
adds clutter, and a one-song artist is for Grey to look at (a spreadsheet).

It names only songs that STILL have no album, never overwrites one, and records
every name as ours: a COLLECTION_ALBUM_NAMED event (old_value empty, new_value the
name), so a real album found later can replace it, and the whole thing can be
found and undone. It changes the CATALOGUE only; the next Act 3's tagger writes
the album into each master and organize files it into the album's folder, and
the next edition build follows.

    python3 scripts/name_collection_albums.py            # list what it would do
    python3 scripts/name_collection_albums.py --execute

Run it when no musaeus process is running: the next Act 3 moves masters.
"""

from __future__ import annotations

import argparse
import collections
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from musaeus.config import MusicConfig  # noqa: E402

EMPTY = "(album IS NULL OR TRIM(album) = '')"
#: Artists whose collection has a name of its own (Grey, 2026-10-04: "as far as I
#: know, they only put out two CDs, both soundtrack albums from the movie").
SPECIAL = {"The Commitments": "The Commitments Soundtrack"}


@dataclass
class Proposal:
    artist: str
    genre: str
    album: str
    songs: int


def collection_name(artist: str, genre: str) -> str:
    if artist in SPECIAL:
        return SPECIAL[artist]
    if genre == "Classical":
        return f"{artist}: Collected Works"
    return f"{artist} Collection"


def proposals(conn: sqlite3.Connection) -> list[Proposal]:
    count: collections.Counter = collections.Counter()
    genre: dict[str, str] = {}
    for r in conn.execute(
        f"SELECT artist, genre FROM archive WHERE status='CATALOGUED' AND {EMPTY} AND TRIM(COALESCE(artist,'')) <> ''"
    ):
        count[r["artist"]] += 1
        genre.setdefault(r["artist"], r["genre"] or "")
    return [
        Proposal(a, genre[a], collection_name(a, genre[a]), n)
        for a, n in sorted(count.items(), key=lambda kv: (-kv[1], kv[0].casefold()))
        if n >= 2
    ]


def apply(conn: sqlite3.Connection, props: list[Proposal]) -> int:
    run_id = f"collection_albums_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    named = 0
    for p in props:
        rows = conn.execute(
            f"SELECT id, file_path FROM archive WHERE status='CATALOGUED' AND artist=? AND {EMPTY}",
            (p.artist,),
        ).fetchall()
        for r in rows:
            if conn.execute(
                f"UPDATE archive SET album=? WHERE id=? AND {EMPTY}", (p.album, r["id"])
            ).rowcount:
                named += 1
                conn.execute(
                    "INSERT INTO events (run_id, ts, event_type, file_path, old_value, new_value, stage, note) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (run_id, ts, "COLLECTION_ALBUM_NAMED", r["file_path"], None, p.album, "collection-albums",
                     "placeholder album chosen by Grey 2026-10-04: no real album was found; replace when one is"),
                )  # fmt: skip
    conn.commit()
    return named


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--execute", action="store_true", help="name them (default: list only)")
    args = ap.parse_args()
    cfg = MusicConfig.from_env()
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000")
    props = proposals(conn)
    print(f"{len(props):,} artist(s), {sum(p.songs for p in props):,} song(s):")
    for p in props:
        print(f"  {p.songs:3}  {p.album}   [{p.genre}]")
    if not args.execute:
        print("\nLIST ONLY: nothing changed. Pass --execute to name them.")
        return 0
    print(
        f"\nNAMED {apply(conn, props):,} song(s). Next: musaeus run --act 3, then the edition builds."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
