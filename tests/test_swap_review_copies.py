"""swap_review_copies: a better copy in duplicate review replaces its master.

Three steps so the song is never absent: --promote hands the review copy to
Act 3 carrying the master's filing facts; Act 3 finalizes it beside the
master; --retire hands the replaced master to delete_reviewed_tracks.py.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "swap_review_copies.py"
COLS = (
    "id INTEGER PRIMARY KEY, file_path TEXT, audio_hash TEXT, artist TEXT, album TEXT, "
    "title TEXT, genre TEXT, codec TEXT, sample_rate INTEGER, duration REAL, status TEXT, "
    "mb_artist_name TEXT, mb_artist_id TEXT, canonicalized_at TEXT, finalized_at TEXT"
)


def _load():
    spec = importlib.util.spec_from_file_location("swap_review_copies", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def vault(tmp_path, monkeypatch):
    mod = _load()
    masters = tmp_path / "Libraries" / "ALAC-Archival"
    review_dir = tmp_path / "REVIEW" / "DUPES_MOVED" / "2026-10-02"
    conn = sqlite3.connect(tmp_path / "musaeus.db")
    conn.row_factory = sqlite3.Row
    conn.execute(f"CREATE TABLE archive ({COLS})")
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, run_id TEXT, ts TEXT, event_type TEXT, "
        "file_path TEXT, old_value TEXT, new_value TEXT, stage TEXT, note TEXT)"
    )
    conn.execute(
        "CREATE TABLE duplicates (id INTEGER PRIMARY KEY, group_id TEXT, file_path TEXT, "
        "status TEXT, audio_hash TEXT)"
    )
    on_disk: dict[str, str] = {}  # path -> the audio hash its file really carries
    monkeypatch.setattr(mod, "audio_hash_safe", lambda p: (on_disk.get(str(p)), None))

    def row(path: Path, status: str, h: str, **kw) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(h.encode())
        on_disk[str(path)] = h
        fields = {
            "file_path": str(path), "audio_hash": h, "status": status, "artist": "Toto", "album": "IV",
            "title": "Rosanna", "genre": "Rock", "codec": "alac", "sample_rate": 44100, "duration": 331.0,
            "finalized_at": "2026-09-25 22:00:00",
        }  # fmt: skip
        fields.update(kw)
        cur = conn.execute(
            f"INSERT INTO archive ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
            tuple(fields.values()),
        )
        conn.commit()
        return cur.lastrowid

    def set_aside(review_path: Path, kept: Path) -> None:
        conn.execute(
            "INSERT INTO events (event_type, file_path, new_value, note) VALUES "
            "('DUPE_MOVED_FOR_REVIEW', ?, ?, ?)",
            (str(review_path), str(review_path), f"group=near_1 type=NEAR kept={kept}"),
        )
        conn.commit()

    cfg = SimpleNamespace(
        alac_archive=masters, runs_root=tmp_path / "RUNS", db_path=tmp_path / "musaeus.db"
    )
    return SimpleNamespace(
        mod=mod, conn=conn, cfg=cfg, row=row, set_aside=set_aside, masters=masters,
        review_dir=review_dir, on_disk=on_disk,
    )  # fmt: skip


def _lib_and_better_review(v, **review_kw):
    lib_path = v.masters / "Rock/Toto/Toto IV/Toto - Rosanna.m4a"
    lib = v.row(
        lib_path, "CATALOGUED", "L", album="Toto IV", mb_artist_name="Toto", mb_artist_id="t1"
    )
    rev_path = v.review_dir / "Toto/My playlist/Toto - Rosanna.m4a"
    kw = {"album": "My playlist", "artist": "TOTO", "sample_rate": 96000}
    kw.update(review_kw)
    rev = v.row(rev_path, "DUPE_REVIEW", "R", **kw)
    v.set_aside(rev_path, lib_path)
    return lib, rev, lib_path, rev_path


def test_a_better_review_copy_is_found_with_its_master(vault):
    lib, rev, _, _ = _lib_and_better_review(vault)
    wins, tally = vault.mod.review_wins(vault.conn)
    assert [(p.review["id"], p.library["id"], p.step) for p in wins] == [(rev, lib, "quality")]
    assert tally["review (quality)"] == 1


def test_a_worse_review_copy_is_not_promoted(vault):
    _lib_and_better_review(vault, sample_rate=44100, title="Rosanna (Live)")
    wins, tally = vault.mod.review_wins(vault.conn)
    assert wins == [] and tally["library (studio/live)"] == 1


def test_promote_dry_run_changes_nothing(vault):
    _, rev, _, _ = _lib_and_better_review(vault)
    vault.mod.promote(vault.conn, vault.mod.review_wins(vault.conn)[0], execute=False)
    assert (
        vault.conn.execute("SELECT status FROM archive WHERE id=?", (rev,)).fetchone()[0]
        == "DUPE_REVIEW"
    )


def test_promote_hands_the_copy_to_act_3_with_the_masters_filing_facts(vault):
    lib, rev, _, rev_path = _lib_and_better_review(vault)
    out = vault.mod.promote(vault.conn, vault.mod.review_wins(vault.conn)[0], execute=True)
    assert out["promoted"] == 1
    r = vault.conn.execute("SELECT * FROM archive WHERE id=?", (rev,)).fetchone()
    assert r["status"] == "CATALOGUED"
    assert (r["artist"], r["album"], r["genre"], r["mb_artist_name"], r["mb_artist_id"]) == (
        "Toto", "Toto IV", "Rock", "Toto", "t1",
    )  # fmt: skip
    assert r["title"] == "Rosanna", "the copy keeps its own title"
    assert r["canonicalized_at"] is None and r["finalized_at"] is None, "Act 3 selects exactly this"
    assert r["file_path"] == str(rev_path), "promote moves no file; finalize does"
    ev = vault.conn.execute(
        "SELECT old_value, new_value FROM events WHERE event_type='SWAP_PROMOTED'"
    ).fetchone()
    assert (int(ev[0]), int(ev[1])) == (lib, rev)
    assert (
        vault.conn.execute("SELECT status FROM archive WHERE id=?", (lib,)).fetchone()[0]
        == "CATALOGUED"
    )


def test_a_review_copy_whose_audio_changed_is_refused(vault):
    _, rev, _, rev_path = _lib_and_better_review(vault)
    vault.on_disk[str(rev_path)] = "SOMETHING ELSE"
    out = vault.mod.promote(vault.conn, vault.mod.review_wins(vault.conn)[0], execute=True)
    assert out == {"refused": 1}
    assert (
        vault.conn.execute("SELECT status FROM archive WHERE id=?", (rev,)).fetchone()[0]
        == "DUPE_REVIEW"
    )


def test_the_master_is_found_after_a_rename(vault):
    lib_path_then = vault.masters / "Rock/Toto Band/Toto IV/Toto Band - Rosanna.m4a"
    lib, rev, lib_path, rev_path = _lib_and_better_review(vault)
    vault.conn.execute("UPDATE events SET note=? WHERE event_type='DUPE_MOVED_FOR_REVIEW'",
                       (f"group=near_1 type=NEAR kept={lib_path_then}",))  # fmt: skip
    vault.conn.execute(
        "INSERT INTO events (event_type, old_value, new_value) VALUES ('ARTIST_CONSOLIDATED', ?, ?)",
        (str(lib_path_then), str(lib_path)),
    )
    vault.conn.commit()
    wins, _ = vault.mod.review_wins(vault.conn)
    assert [p.library["id"] for p in wins] == [lib]


def test_retire_waits_for_act_3_then_hands_the_master_to_the_delete_tool(vault, monkeypatch):
    lib, rev, _, _ = _lib_and_better_review(vault)
    vault.mod.promote(vault.conn, vault.mod.review_wins(vault.conn)[0], execute=True)
    assert vault.mod.to_retire(vault.conn, vault.masters) == [], "not finalized yet"

    new_home = vault.masters / "Rock/Toto/Toto IV/Toto - Rosanna (2).m4a"
    new_home.write_bytes(b"R")  # what Act 3's finalize does
    vault.conn.execute(
        "UPDATE archive SET file_path=?, finalized_at='2026-10-04 01:00:00', "
        "canonicalized_at='2026-10-04 00:59:00' WHERE id=?",
        (str(new_home), rev),
    )
    vault.conn.commit()
    assert vault.mod.to_retire(vault.conn, vault.masters) == [lib]

    calls = []
    monkeypatch.setattr(
        vault.mod.subprocess,
        "run",
        lambda cmd, **k: calls.append(cmd) or SimpleNamespace(returncode=0),
    )
    assert vault.mod.retire(vault.cfg, [lib], execute=False) == 0
    cmd = calls[0]
    assert Path(cmd[1]).name == "delete_reviewed_tracks.py" and "--execute" not in cmd
    assert Path(cmd[2]).read_text() == f"{lib}\n"
