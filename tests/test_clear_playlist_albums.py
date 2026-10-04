"""clear_playlist_albums: the catalogue's playlist-named albums are cleared, only those."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "clear_playlist_albums.py"


@pytest.fixture
def db():
    spec = importlib.util.spec_from_file_location("clear_playlist_albums", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE archive (id INTEGER PRIMARY KEY, file_path TEXT, artist TEXT, title TEXT, "
        "album TEXT, status TEXT)"
    )
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, run_id TEXT, ts TEXT, event_type TEXT, "
        "file_path TEXT, old_value TEXT, new_value TEXT, stage TEXT, note TEXT)"
    )
    for i, (album, status) in enumerate(
        [
            ("My playlist S", "CATALOGUED"),
            ("my playlist", "CATALOGUED"),
            ("60's British Invasion Playlist", "CATALOGUED"),
            ("Toto IV", "CATALOGUED"),
            ("Playlist: The Very Best of ABBA", "CATALOGUED"),
            (None, "CATALOGUED"),
            ("My playlist S", "DUPE_REVIEW"),
        ],
        1,
    ):
        conn.execute(
            "INSERT INTO archive VALUES (?,?,?,?,?,?)",
            (i, f"/f{i}.m4a", "A", f"T{i}", album, status),
        )
    return mod, conn


def _albums(conn):
    return dict(conn.execute("SELECT id, album FROM archive").fetchall())


def test_only_catalogued_playlist_names_are_found(db):
    mod, conn = db
    assert [r["id"] for r in mod.playlist_rows(conn)] == [1, 2, 3]


def test_clear_empties_those_and_records_each(db):
    mod, conn = db
    assert mod.clear(conn, mod.playlist_rows(conn)) == 3
    a = _albums(conn)
    assert (a[1], a[2], a[3]) == (None, None, None)
    assert a[4] == "Toto IV" and a[5] == "Playlist: The Very Best of ABBA", "real albums stay"
    assert a[7] == "My playlist S", "a review copy is not the library's business"
    ev = conn.execute(
        "SELECT old_value FROM events WHERE event_type='ALBUM_CLEARED' ORDER BY id"
    ).fetchall()
    assert [e[0] for e in ev] == ["My playlist S", "my playlist", "60's British Invasion Playlist"]


def test_a_second_run_finds_nothing(db):
    mod, conn = db
    mod.clear(conn, mod.playlist_rows(conn))
    assert mod.playlist_rows(conn) == []
