"""Review of slice E (#144), findings 1-5: `rebuild-from-disk --promote`.

1. It held no lock: a run in progress went on writing by row id into the
   renumbered table, one song's title or composer onto another.
2. Its only check was "more than 0 rows": a --limit rebuild, or one of a drive
   only partly there, replaced the whole catalogue.
3. promote committed each step on its own: a stop between the two renames left
   no `archive` table. And a second promote in the same second failed.
4. It dropped the person's keeps (`kept_by_person_at`), which nothing on disk
   records, so the next Act 2 would move copies Grey chose to keep.
5. It dropped every row the scan of the masters cannot see: STAGING's PENDING
   and HASHED, QUARANTINED, GHOST, DELETED.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from datetime import datetime, timezone

import pytest

from musaeus import cli, rebuild_from_disk
from musaeus.config import MusicConfig
from musaeus.db import open_db, upsert_archive
from musaeus.masters_lock import EXIT_BUSY, masters_lock
from musaeus.rebuild_from_disk import create_rebuild_table, promote


def _rebuilt(conn, *rows):
    create_rebuild_table(conn, "archive_rebuilt")
    for path in rows:
        conn.execute(
            "INSERT INTO archive_rebuilt (file_path, status) VALUES (?, 'CATALOGUED')", (path,)
        )
    conn.commit()


def _paths(conn, table="archive"):
    return sorted(r[0] for r in conn.execute(f"SELECT file_path FROM {table}"))


def _backups(conn):
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'archive_pre_rebuild_%'"
        )
    ]


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "musaeus.db")
    for path, status in (("/m/a.m4a", "CATALOGUED"), ("/m/b.m4a", "CATALOGUED")):
        upsert_archive(c, {"file_path": path, "status": status})
    c.commit()
    yield c
    c.close()


def test_a_rebuild_missing_a_catalogued_file_is_not_promoted(conn):
    _rebuilt(conn, "/m/a.m4a")  # b's drive was not there
    with contextlib.suppress(Exception):  # refused (the old code had nothing to refuse with)
        promote(conn)
    assert _paths(conn) == ["/m/a.m4a", "/m/b.m4a"], "b's row was dropped"
    assert promote(conn, accept_missing=True)  # asked for: it goes ahead
    assert _paths(conn) == ["/m/a.m4a"]


def test_a_promote_stopped_part_way_leaves_the_archive(conn, tmp_path):
    _rebuilt(conn, "/m/a.m4a", "/m/b.m4a")

    def stop_at_the_second_rename(action, arg1, arg2, *rest):
        if action == sqlite3.SQLITE_ALTER_TABLE and arg2 == "archive_rebuilt":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(stop_at_the_second_rename)
    with pytest.raises(sqlite3.DatabaseError):
        promote(conn)
    conn.set_authorizer(None)
    conn.close()
    again = open_db(tmp_path / "musaeus.db")
    try:
        assert _paths(again) == ["/m/a.m4a", "/m/b.m4a"], "the catalogue was left empty"
        assert len(again.execute("PRAGMA index_list(archive)").fetchall()) >= 3
    finally:
        again.close()


def test_two_promotes_in_one_second(conn, monkeypatch):
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(rebuild_from_disk, "datetime", _Frozen)
    _rebuilt(conn, "/m/a.m4a", "/m/b.m4a")
    promote(conn)
    _rebuilt(conn, "/m/a.m4a", "/m/b.m4a")
    promote(conn)
    assert len(_backups(conn)) == 2
    assert _paths(conn) == ["/m/a.m4a", "/m/b.m4a"]


def test_the_persons_keeps_survive_a_promote(conn):
    conn.execute("UPDATE archive SET kept_by_person_at='2026-10-09' WHERE file_path='/m/a.m4a'")
    conn.commit()
    _rebuilt(conn, "/m/a.m4a", "/m/b.m4a")
    promote(conn)
    kept = dict(conn.execute("SELECT file_path, kept_by_person_at FROM archive").fetchall())
    assert kept == {"/m/a.m4a": "2026-10-09", "/m/b.m4a": None}


def test_rows_the_scan_cannot_see_survive_a_promote(conn):
    unseen = {
        "/staging/p.flac": "PENDING",
        "/staging/h.flac": "HASHED",
        "/quarantine/q.mp3": "QUARANTINED",
        "/m/gone.m4a": "GHOST",
        "/m/deleted.m4a": "DELETED",
        "/m/back.m4a": "GHOST",  # its file is back: the rebuild's row wins
    }
    for path, status in unseen.items():
        upsert_archive(conn, {"file_path": path, "status": status, "artist": "Kept"})
    conn.commit()
    _rebuilt(conn, "/m/a.m4a", "/m/b.m4a", "/m/back.m4a")
    promote(conn)
    got = {
        r[0]: (r[1], r[2]) for r in conn.execute("SELECT file_path, status, artist FROM archive")
    }
    for path, status in unseen.items():
        if path != "/m/back.m4a":
            assert got.get(path) == (status, "Kept"), f"{path} was dropped or changed"
    assert got["/m/back.m4a"] == ("CATALOGUED", None)


# ── The command ───────────────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path, monkeypatch):
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        db_path=tmp_path / "musaeus.db",
    )
    cfg.ensure_dirs()
    for name in ("a", "b"):
        f = cfg.alac_archive / "Rock" / f"{name}.m4a"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"not really audio")
    c = open_db(cfg.db_path)
    for name in ("a", "b"):
        upsert_archive(c, {"file_path": str(cfg.alac_archive / "Rock" / f"{name}.m4a"),
                           "status": "CATALOGUED", "title": f"{name} title"})  # fmt: skip
    c.commit()
    c.close()
    monkeypatch.setattr(cli, "get_config", lambda: cfg)
    monkeypatch.setattr("musaeus.setup.needs_setup", lambda: False)
    monkeypatch.setenv("MUSAEUS_VERBOSE", "0")
    monkeypatch.setenv("MUSAEUS_PROGRESS", "0")
    return cfg


def _musaeus(monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", ["musaeus", *argv])
    with pytest.raises(SystemExit) as stop:
        cli.main()
    return int(stop.value.code or 0)


def _unchanged(cfg):
    c = sqlite3.connect(str(cfg.db_path))
    try:
        titles = sorted((r[0] or "") for r in c.execute("SELECT title FROM archive"))
        return titles == ["a title", "b title"] and not _backups(c)
    finally:
        c.close()


def test_promote_waits_for_a_job_holding_the_masters(vault, monkeypatch):
    with masters_lock(vault.runs_root, exclusive=False, what="edition-build car"):
        code = _musaeus(monkeypatch, "rebuild-from-disk", "--no-hashes", "--promote")
    assert _unchanged(vault), "the catalogue was swapped under a running job"
    assert code == EXIT_BUSY


def test_a_limited_rebuild_is_never_promoted(vault, monkeypatch):
    code = _musaeus(monkeypatch, "rebuild-from-disk", "--no-hashes", "--limit", "1", "--promote")
    assert _unchanged(vault), "a --limit rebuild replaced the catalogue"
    assert code == 1


def test_a_whole_rebuild_is_promoted(vault, monkeypatch):
    assert _musaeus(monkeypatch, "rebuild-from-disk", "--no-hashes", "--promote") == 0
    c = sqlite3.connect(str(vault.db_path))
    try:
        assert len(_paths(c)) == 2 and len(_backups(c)) == 1
    finally:
        c.close()


# ── Pre-merge review of #160 ──────────────────────────────────────────────────


def test_a_genre_ruling_survives_a_promote(conn):
    from musaeus.db import ensure_columns

    ensure_columns(conn, (("genre_ruled_at", "TEXT"),))
    conn.execute("UPDATE archive SET genre_ruled_at='2026-09-14' WHERE file_path='/m/a.m4a'")
    conn.commit()
    _rebuilt(conn, "/m/a.m4a", "/m/b.m4a")
    promote(conn)
    ruled = dict(conn.execute("SELECT file_path, genre_ruled_at FROM archive").fetchall())
    assert ruled == {"/m/a.m4a": "2026-09-14", "/m/b.m4a": None}


def test_a_plain_rebuild_waits_for_a_promote(vault, monkeypatch):
    with masters_lock(vault.runs_root, exclusive=True, what="musaeus rebuild-from-disk --promote"):
        code = _musaeus(monkeypatch, "rebuild-from-disk", "--no-hashes")
    assert code == EXIT_BUSY
