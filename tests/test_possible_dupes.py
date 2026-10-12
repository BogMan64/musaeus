"""`musaeus possible-dupes` (Grey, 2026-10-11): what the duplicate checks
leave, side by side on a Desktop sheet; read-only."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from musaeus import possible_dupes as pd
from musaeus.db import open_db, upsert_archive


def _song(conn, path, artist, title, length, *, album="A", audio=None):
    upsert_archive(conn, {"file_path": path, "status": "CATALOGUED", "artist": artist,
                          "title": title, "album": album, "duration": length,
                          "codec": "alac", "audio_hash": audio or "pcm:" + path})  # fmt: skip


@pytest.fixture
def conn(tmp_path: Path):
    c = open_db(tmp_path / "m.db")
    yield c
    c.close()


def _titles(pairs):
    return sorted(tuple(sorted((p.a["title"], p.b["title"]))) for p in pairs)


def test_and_against_ampersand_at_one_length_is_listed(conn):
    _song(conn, "/m/1.m4a", "Question Mark & The Mysterians", "Shout (Part 1 & 2)", 180.0)
    _song(conn, "/m/2.m4a", "Question Mark & The Mysterians", "Shout (Part 1 And 2)", 180.0)
    conn.commit()
    pairs = pd.find(conn)
    assert _titles(pairs) == [("Shout (Part 1 & 2)", "Shout (Part 1 And 2)")]
    assert pairs[0].why.startswith("same title")


@pytest.mark.parametrize(
    ("second", "length", "audio"),
    [
        ("Shout (Part 1 And 2)", 240.0, None),  # another length: another recording
        ("Shout (Part 1 And 2)", 180.0, "pcm:/m/1.m4a"),  # the same audio: CrossDupe's
        ("Twist And Shout", 180.0, None),  # another song
    ],
)
def test_what_is_not_listed(conn, second, length, audio):
    _song(conn, "/m/1.m4a", "The Isley Brothers", "Shout (Part 1 & 2)", 180.0)
    _song(conn, "/m/2.m4a", "The Isley Brothers", second, length, audio=audio)
    conn.commit()
    assert pd.find(conn) == []


def test_a_pair_kept_both_is_never_offered_again(conn):
    _song(conn, "/m/1.m4a", "John Mellencamp", "Jack & Diane", 254.0)
    _song(conn, "/m/2.m4a", "John Mellencamp", "Jack And Diane", 255.0)
    for path in ("/m/1.m4a", "/m/2.m4a"):
        conn.execute("INSERT INTO duplicates (group_id, file_path, duplicate_type, status, audio_hash) "
                     "VALUES ('near_1', ?, 'NEAR', 'keep', ?)", (path, "pcm:" + path))  # fmt: skip
    conn.commit()
    assert pd.find(conn) == []


def test_a_shared_credit_bracket_does_not_make_two_songs_one(conn):
    _song(
        conn,
        "/m/1.m4a",
        "Billie Holiday",
        "Easy Living (With Teddy Wilson And His Orchestra)",
        190.0,
    )
    _song(conn, "/m/2.m4a", "Billie Holiday", "My Man (With Teddy Wilson And His Orchestra)", 190.3)
    conn.commit()
    assert pd.find(conn) == []


def test_two_live_nights_are_two_songs(conn):
    _song(conn, "/m/1.m4a", "Cream", "Crossroads (Live)", 250.0, album="Wheels Of Fire")
    _song(conn, "/m/2.m4a", "Cream", "Crossroads (Live)", 256.0, album="Live Cream")
    conn.commit()
    assert pd.find(conn) == []


def test_one_act_under_a_credit_is_compared(conn):
    _song(conn, "/m/1.m4a", "Rod Stewart", "Bewitched, Bothered & Bewildered", 200.0)
    _song(conn, "/m/2.m4a", "Rod Stewart feat. Cher", "Bewitched, Bothered And Bewildered", 200.0)
    conn.commit()
    assert len(pd.find(conn)) == 1


def test_the_sheet_and_the_catalogue(tmp_path, conn):
    _song(conn, "/lib/Rock/A/1.m4a", "José Feliciano", "Chico & The Man", 150.0)
    _song(conn, "/lib/Rock/A/2.m4a", "José Feliciano", "Chico And The Man", 150.0)
    conn.commit()
    before = conn.execute("SELECT * FROM archive ORDER BY file_path").fetchall()
    sheet = tmp_path / "sheet.csv"
    pd.write_sheet(pd.find(conn), sheet, [Path("/lib")])
    with open(sheet, encoding="utf-8-sig", newline="") as fh:
        (row,) = list(csv.DictReader(fh))
    assert row["file_a"] == "Rock/A/1.m4a" and row["length_a"] == "2:30" and row["decide"] == ""
    assert conn.execute("SELECT * FROM archive ORDER BY file_path").fetchall() == before
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".part")]


def test_it_opens_the_catalogue_read_only(tmp_path, monkeypatch, conn):
    """cmd_possible_dupes connects with mode=ro; a write through it fails."""
    conn.commit()
    ro = sqlite3.connect(f"file:{tmp_path / 'm.db'}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("UPDATE archive SET title = 'x'")
    ro.close()


# ── Pre-merge review of #191 ─────────────────────────────────────────────────


def _group(conn, gid, paths, status="keep", kind="NEAR", hashes=None):
    for i, p in enumerate(paths):
        h = (hashes or [None] * len(paths))[i]
        conn.execute("INSERT INTO duplicates (group_id, file_path, duplicate_type, status, audio_hash) "
                     "VALUES (?, ?, ?, ?, ?)", (gid, p, kind, status, h))  # fmt: skip


def test_a_pair_kept_both_stays_decided_when_a_fingerprint_changed(conn):
    _song(conn, "/m/1.m4a", "Simon & Garfunkel", "Overs", 130.0, audio="pcm:new")
    _song(conn, "/m/2.m4a", "Simon & Garfunkel", "Overs", 131.0)
    _group(conn, "near_x", ["/m/1.m4a", "/m/2.m4a"], hashes=["pcm:old", "pcm:/m/2.m4a"])
    conn.commit()
    assert pd.find(conn) == []


@pytest.mark.parametrize(
    ("first", "second", "lengths"),
    [
        ("Overs", "Overs (Live)", (130.0, 133.9)),  # studio against a live take
        ("The Future's So Bright", "The Future's So Bright (Re-Recorded)", (180.0, 182.0)),
        ("Loca (feat. Dizzee Rascal)", "Loca (feat. El Cata)", (190.0, 196.0)),  # two guests
    ],
)
def test_not_one_recording(conn, first, second, lengths):
    _song(conn, "/m/1.m4a", "Some Act", first, lengths[0])
    _song(conn, "/m/2.m4a", "Some Act", second, lengths[1])
    conn.commit()
    assert pd.find(conn) == []


@pytest.mark.parametrize(
    "edit", ["(Single)", "(Album Edit)", "(Video)", "(Original Mix)", "(Bonus Track)"]
)
def test_an_edit_bracket_hides_nothing(conn, edit):
    _song(conn, "/m/1.m4a", "War", "Slippin' Into Darkness", 420.0)
    _song(conn, "/m/2.m4a", "War", f"Slippin' Into Darkness {edit}", 420.0)
    conn.commit()
    assert len(pd.find(conn)) == 1


def test_an_acoustid_pair_waiting_for_grey_says_so(conn):
    _song(conn, "/m/1.m4a", "John Mellencamp", "Jack & Diane", 254.0)
    _song(conn, "/m/2.m4a", "John Mellencamp", "Jack And Diane", 255.0)
    _group(conn, "acoustic_1", ["/m/1.m4a", "/m/2.m4a"], status="review", kind="ACOUSTIC")
    conn.commit()
    (pair,) = pd.find(conn)
    assert pair.why.startswith("AcoustID: same recording, waiting for you")
