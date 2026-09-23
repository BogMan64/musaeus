"""Lifting a deny-list entry when a ruling is reversed.

2026-09-23: 170 tracks Grey had ruled out were re-admitted by a rebuild run
with --skip deny-list, and on review he decided to keep them. Kept tracks
whose audio stays denied are refused on the next normal rebuild, and doctor
reports every one of them as removed audio still held. The ledger keeps no
history of its own, so a lift that does not record what it lifted erases the
reason the audio was denied in the first place -- the event log has to carry
it.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from musaeus.db import deny_hash, ensure_deny_list, open_hash_index, undeny_hash

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "undeny_hashes.py"


def _ledger(path: Path) -> sqlite3.Connection:
    conn = open_hash_index(path)
    ensure_deny_list(conn)
    return conn


class TestUndenyHash:
    def test_it_lifts_the_entry_and_returns_what_it_said(self, tmp_path):
        conn = _ledger(tmp_path / "hash_index.db")
        deny_hash(conn, "h1", "ToBeDeleted folder, ruled by Grey", "/x/a.m4a")
        lifted = undeny_hash(conn, "h1")
        assert lifted is not None and lifted["reason"] == "ToBeDeleted folder, ruled by Grey"
        assert (
            conn.execute("SELECT COUNT(*) FROM denied_hashes WHERE audio_hash='h1'").fetchone()[0]
            == 0
        )

    def test_audio_that_was_never_denied_returns_none(self, tmp_path):
        conn = _ledger(tmp_path / "hash_index.db")
        assert undeny_hash(conn, "never") is None

    def test_other_entries_are_left_alone(self, tmp_path):
        conn = _ledger(tmp_path / "hash_index.db")
        deny_hash(conn, "h1", "r1")
        deny_hash(conn, "h2", "r2")
        undeny_hash(conn, "h1")
        # open_hash_index returns sqlite3.Row, so compare values, not tuples.
        assert [r[0] for r in conn.execute("SELECT audio_hash FROM denied_hashes")] == ["h2"]


@pytest.fixture
def vault(tmp_path, monkeypatch):
    (tmp_path / "_db_backups").mkdir()
    db = tmp_path / "musaeus.db"
    c = sqlite3.connect(db)
    c.execute(
        "CREATE TABLE archive (id INTEGER PRIMARY KEY, artist TEXT, title TEXT, status TEXT, audio_hash TEXT, file_path TEXT)"
    )
    c.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, ts TEXT, event_type TEXT, file_path TEXT, old_value TEXT, new_value TEXT, note TEXT)"
    )
    c.executemany(
        "INSERT INTO archive VALUES (?,?,?,'CATALOGUED',?,?)",
        [
            (1, "Santana", "Europa", "hA", "/lib/a.m4a"),
            (2, "Eagles", "Hotel California", "hB", "/lib/b.m4a"),
            (3, "Queen", "Bicycle Race", "hC", "/lib/c.m4a"),
        ],
    )
    c.commit()
    c.close()
    led = _ledger(tmp_path / "_db_backups" / "hash_index.db")
    deny_hash(led, "hA", "ToBeDeletedII folder, ruled by Grey", "/old/a.m4a")
    deny_hash(led, "hB", "live version superseded", "/old/b.m4a")
    led.commit()
    led.close()
    monkeypatch.setenv("MUSAEUS_VAULT_ROOT", str(tmp_path))
    monkeypatch.setenv("MUSAEUS_DB", str(db))
    return tmp_path


def _run(vault, ids, execute=True):
    f = vault / "ids.txt"
    f.write_text("\n".join(str(i) for i in ids))
    cmd = [sys.executable, str(SCRIPT), str(f), "--reason", "kept after review"]
    if execute:
        cmd.append("--execute")
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=120, cwd=str(SCRIPT.parents[1])
    )


def _denied(vault):
    c = sqlite3.connect(vault / "_db_backups" / "hash_index.db")
    out = {h for (h,) in c.execute("SELECT audio_hash FROM denied_hashes")}
    c.close()
    return out


class TestTheScript:
    def test_a_dry_run_changes_nothing(self, vault):
        r = _run(vault, [1, 2], execute=False)
        assert r.returncode == 0, r.stderr
        assert _denied(vault) == {"hA", "hB"}

    def test_it_lifts_only_the_named_tracks(self, vault):
        r = _run(vault, [1])
        assert r.returncode == 0, r.stderr
        assert _denied(vault) == {"hB"}

    def test_the_event_log_keeps_the_ruling_that_was_lifted(self, vault):
        _run(vault, [1])
        c = sqlite3.connect(vault / "musaeus.db")
        note = c.execute(
            "SELECT note FROM events WHERE event_type='UNDENIED_BY_REVIEW' AND file_path='/lib/a.m4a'"
        ).fetchone()
        c.close()
        assert note is not None, "the lift left no record"
        assert "ToBeDeletedII" in note[0], "the record must say what had been ruled"

    def test_a_track_whose_audio_was_not_denied_is_reported_not_invented(self, vault):
        r = _run(vault, [3])
        assert r.returncode == 0, r.stderr
        assert "not on the deny list" in r.stdout
        c = sqlite3.connect(vault / "musaeus.db")
        assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        c.close()
