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


def test_auto_leaves_the_group_to_the_resolver(conn, monkeypatch):
    """Review of #123 (2026-10-08): the resolver obeys a person's keep and
    archive, and auto wrote them from the console's own ranking, which lacked
    what the resolver ranks by. Auto now writes nothing: the resolver applies
    the keep rule itself (Grey, 2026-10-09: "leave it to the resolver")."""
    monkeypatch.setattr("sys.stdin", io.StringIO("A\n"))
    dedupe.run_dedupe_console(conn)
    assert set(_statuses(conn).values()) == {"pending"}


def test_auto_mode_leaves_every_group_to_the_resolver(conn, capsys):
    dedupe.run_dedupe_console(conn, auto_mode=True)
    assert set(_statuses(conn).values()) == {"pending"}
    assert "keep rule" in capsys.readouterr().out


def test_the_console_ranks_the_filed_copy_first_as_the_resolver_does(conn):
    """Equally good copies: the one already filed stays. Without finalized_at
    the console ranked a new arrival with a few more bytes first."""
    for path in ("/m/filed.m4a", "/m/arrival.m4a"):
        upsert_archive(conn, {"file_path": path, "status": "CATALOGUED", "artist": "A",
                              "title": "Other", "album": "Album", "codec": "alac",
                              "bitrate": 900_000, "duration": 200.0, "sample_rate": 44100,
                              "size_bytes": 30_000_000 + (500 if path == "/m/arrival.m4a" else 0),
                              "audio_hash": "same"})  # fmt: skip
        conn.execute(
            "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, run_id) "
            "VALUES ('dup_same', ?, 'EXACT', 1.0, 'r')", (path,))  # fmt: skip
    conn.execute("UPDATE archive SET finalized_at = '2026-10-01' WHERE file_path = '/m/filed.m4a'")
    conn.commit()
    from musaeus.stages import dupe_resolver

    console = [m["file_path"] for m in dedupe._get_group_members(conn, "dup_same")]
    resolver = [m["file_path"] for m in dupe_resolver._get_group_members(conn, "dup_same")]
    assert console[0] == "/m/filed.m4a"
    assert console == resolver


def test_the_numbers_stay_on_the_files_shown(tmp_path, monkeypatch, capsys):
    """Review of #129-#134, finding 1 (reproduced): after "1a" the group was
    re-sorted (an archived copy ranks last) but not shown again, so "3k" -- meant
    for the third file shown -- kept the copy just archived."""
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q", runs_root=tmp_path / "RUNS", meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library", db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    conn = open_db(cfg.db_path)
    for path, rate in (("/m/a.m4a", 900_000), ("/m/b.m4a", 800_000), ("/m/c.m4a", 700_000)):
        upsert_archive(conn, {"file_path": path, "status": "CATALOGUED", "artist": "A",
                              "title": "Song", "album": "Album", "codec": "alac", "bitrate": rate,
                              "size_bytes": 1000, "duration": 200.0, "sample_rate": 44100,
                              "audio_hash": path})  # fmt: skip
        conn.execute(
            "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, run_id) "
            "VALUES ('near_x', ?, 'NEAR', 0.9, 'r')", (path,))  # fmt: skip
    conn.commit()
    shown = [m["file_path"] for m in dedupe._get_group_members(conn, "near_x")]
    assert shown == ["/m/a.m4a", "/m/b.m4a", "/m/c.m4a"]
    monkeypatch.setattr("sys.stdin", io.StringIO("1a\n3k\nq\n"))
    dedupe.run_dedupe_console(conn)
    s = _statuses(conn)
    assert s["/m/a.m4a"] == dedupe.ARCHIVE_USER, "the copy archived first was kept"
    assert s["/m/c.m4a"] == dedupe.KEEP_USER
    assert s["/m/b.m4a"] == "pending"
    out = capsys.readouterr().out
    assert out.count("/m/c.m4a") >= 2, "the group is shown again after each choice"
