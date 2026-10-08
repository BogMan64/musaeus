"""Review of #86, finding 9 (2026-10-07), in `musaeus dedupe`.

* Every key was lowercased, so "A" (auto) and "a" became one: typing "a", which
  the help calls "archive this file", auto-resolved the whole group.
* Auto kept the copy its own order picked (lossless first, then bitrate and
  size), not Grey's keep rule, so it could keep a copy the resolver would not.
"""

from __future__ import annotations

import io

import pytest

from musaeus import dedupe
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive


@pytest.fixture
def conn(tmp_path):
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q", runs_root=tmp_path / "RUNS", meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library", db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    ctx = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    for path, album, size in (("/m/studio.m4a", "Album", 30_000_000),
                              ("/m/live.m4a", "Live at the Fillmore", 40_000_000)):  # fmt: skip
        upsert_archive(ctx.conn, {"file_path": path, "status": "CATALOGUED", "artist": "A",
                                  "title": "Song", "album": album, "codec": "alac",
                                  "bitrate": 900_000, "size_bytes": size, "duration": 200.0,
                                  "sample_rate": 44100, "audio_hash": path})  # fmt: skip
        ctx.conn.execute(
            "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, run_id) "
            "VALUES ('near_x', ?, 'NEAR', 0.9, 'r')", (path,))  # fmt: skip
    ctx.conn.commit()
    return ctx.conn


def _statuses(conn) -> dict[str, str]:
    return dict(conn.execute("SELECT file_path, status FROM duplicates").fetchall())


def test_a_bare_a_does_not_auto_resolve_the_group(conn, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("a\nq\n"))
    dedupe.run_dedupe_console(conn)
    assert set(_statuses(conn).values()) == {"pending"}, "'a' auto-resolved the group"


def test_auto_keeps_what_the_keep_rule_keeps(conn, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("A\n"))
    dedupe.run_dedupe_console(conn)
    s = _statuses(conn)
    # A person's auto is still a person's decision, for the resolver to carry out (#123).
    assert s["/m/studio.m4a"] == dedupe.KEEP_USER, (
        "auto kept the bigger live copy over the studio one"
    )
    assert s["/m/live.m4a"] == dedupe.ARCHIVE_USER
