"""name_collection_albums: an artist with two or more songs and no album gets a collection (Grey, 2026-10-04)."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "name_collection_albums.py"


@pytest.fixture
def env():
    spec = importlib.util.spec_from_file_location("name_collection_albums", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE archive (id INTEGER PRIMARY KEY, file_path TEXT, artist TEXT, title TEXT, "
        "album TEXT, genre TEXT, status TEXT)"
    )
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, run_id TEXT, ts TEXT, event_type TEXT, "
        "file_path TEXT, old_value TEXT, new_value TEXT, stage TEXT, note TEXT)"
    )

    def add(artist, genre, n=1, album=None, status="CATALOGUED"):
        for i in range(n):
            conn.execute(
                "INSERT INTO archive (file_path, artist, title, album, genre, status) VALUES (?,?,?,?,?,?)",
                (f"/{artist}-{i}.m4a", artist, f"Song {i}", album, genre, status),
            )
        conn.commit()

    return mod, conn, add


def _albums(conn):
    return {r["artist"]: {x["album"] for x in conn.execute("SELECT album FROM archive WHERE artist=?", (r["artist"],))}
            for r in conn.execute("SELECT DISTINCT artist FROM archive")}  # fmt: skip


def test_names(env):
    mod, conn, add = env
    add("Louis Armstrong", "Jazz", 3)
    add("Johann Sebastian Bach", "Classical", 2)
    add("The Commitments", "Soundtrack", 4)
    names = {p.artist: p.album for p in mod.proposals(conn)}
    assert names == {
        "Louis Armstrong": "Louis Armstrong Collection",
        "Johann Sebastian Bach": "Johann Sebastian Bach: Collected Works",
        "The Commitments": "The Commitments Soundtrack",
    }


def test_one_song_artists_are_left_alone_and_so_are_songs_that_have_an_album(env):
    mod, conn, add = env
    add("One Hit Wonder", "Rock", 1)
    add("Toto", "Rock", 2, album="Toto IV")
    add("Review Copy", "Rock", 3, status="DUPE_REVIEW")
    add("Benny Goodman", "Jazz", 2)
    add("Benny Goodman", "Jazz", 1, album="Live at Carnegie Hall")
    assert [p.artist for p in mod.proposals(conn)] == ["Benny Goodman"]
    assert [p.songs for p in mod.proposals(conn)] == [2], "only the songs that have no album"


def test_apply_names_the_songs_and_records_each_as_ours(env):
    mod, conn, add = env
    add("Louis Armstrong", "Jazz", 2)
    add("One Hit Wonder", "Rock", 1)
    assert mod.apply(conn, mod.proposals(conn)) == 2
    a = _albums(conn)
    assert a["Louis Armstrong"] == {"Louis Armstrong Collection"} and a["One Hit Wonder"] == {None}
    ev = conn.execute(
        "SELECT old_value,new_value,note FROM events WHERE event_type='COLLECTION_ALBUM_NAMED'"
    ).fetchall()
    assert len(ev) == 2 and all(
        e["old_value"] is None and e["new_value"] == "Louis Armstrong Collection" for e in ev
    )
    assert "placeholder" in ev[0]["note"]


def test_a_second_run_finds_nothing(env):
    mod, conn, add = env
    add("Louis Armstrong", "Jazz", 2)
    mod.apply(conn, mod.proposals(conn))
    assert mod.proposals(conn) == []


def test_an_album_set_meanwhile_is_never_overwritten(env):
    mod, conn, add = env
    add("Louis Armstrong", "Jazz", 2)
    props = mod.proposals(conn)
    conn.execute("UPDATE archive SET album='Hot Fives' WHERE id=1")
    mod.apply(conn, props)
    assert [r["album"] for r in conn.execute("SELECT album FROM archive ORDER BY id")] == [
        "Hot Fives",
        "Louis Armstrong Collection",
    ]
