"""Findings 3 and 13 of the review of #87 (2026-10-07): the monthly music backup.

3. A copy that failed its check kept its final dated name. The next month it
   counted as one of the two kept copies, so rotation deleted the last good one,
   and the bit-rot repair could take a song from it.
13. The check compared size and modification time only, so a damaged copy with
   both intact passed; hash_index.db (the deny list) was copied raw and never
   checked.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from musaeus import bitrot_repair
from musaeus import music_backup as mb

pytestmark = pytest.mark.skipif(not shutil.which("rsync"), reason="requires rsync")
REL = Path("Rock/America/Hearts/America - Sister Golden Hair.m4a")


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "VAULT"
    p = v / "Libraries" / "ALAC-Archival" / REL
    p.parent.mkdir(parents=True)
    p.write_bytes(b"audio" * 1000)
    (v / "MetaData").mkdir()
    (v / "_db_backups").mkdir()
    for db in (
        v / "musaeus.db",
        v / "_db_backups" / "editions.db",
        v / "_db_backups" / "hash_index.db",
    ):
        sqlite3.connect(db).execute("create table t(x)").connection.commit()
    return v


def test_a_failed_copy_never_pushes_out_the_last_good_one(vault, tmp_path, monkeypatch):
    root = tmp_path / "NUC"
    root.mkdir()
    assert mb.run(vault, root, today="20260901").problems == []
    assert mb.run(vault, root, today="20261001").problems == []

    real = mb.verify_copy
    monkeypatch.setattr(mb, "verify_copy", lambda v, d: (0, ["the drive filled up"]))
    failed = mb.run(vault, root, today="20261101")
    monkeypatch.setattr(mb, "verify_copy", real)
    assert failed.problems and failed.removed == []
    assert not (root / f"{mb.PREFIX}20261101").exists(), "a failed copy took a good copy's name"

    ok = mb.run(vault, root, today="20261201")

    assert ok.problems == []
    assert (root / f"{mb.PREFIX}20261001").is_dir(), "the last good copy was deleted"
    assert [c.name[-8:] for c in mb.dated_copies(root)] == ["20261201", "20261001"]


def test_a_failed_copy_is_never_a_repair_source(tmp_path):
    (tmp_path / "NUC" / f"{mb.PREFIX}20261101.partial" / "ALAC-Archival").mkdir(parents=True)
    (tmp_path / "NUC" / f"{mb.PREFIX}20261001" / "ALAC-Archival").mkdir(parents=True)

    copies = bitrot_repair.backup_copies([tmp_path / "NUC"])

    assert [c.parent.name for c in copies] == [f"{mb.PREFIX}20261001"]


def test_a_damaged_copy_with_the_same_size_and_time_is_caught(vault, tmp_path):
    root = tmp_path / "NUC"
    root.mkdir()
    dest = mb.run(vault, root, today="20261101").dest
    copy = dest / "ALAC-Archival" / REL
    st = copy.stat()
    copy.write_bytes(b"AUDIO" * 1000)  # same size, different bytes
    os.utime(copy, ns=(st.st_atime_ns, st.st_mtime_ns))

    _, problems = mb.verify_copy(vault, dest)

    assert problems, "a damaged copy passed the check"


def test_the_deny_list_is_backed_up_consistently_and_checked(vault, tmp_path):
    root = tmp_path / "NUC"
    root.mkdir()
    dest = mb.run(vault, root, today="20261101").dest
    copy = dest / "vault_state" / "hash_index.db"
    assert copy.is_file(), "hash_index.db was not backed up through SQLite"

    copy.write_bytes(b"not a database" * 100)
    _, problems = mb.verify_copy(vault, dest)
    assert any("hash_index.db" in p for p in problems)
