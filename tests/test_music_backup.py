"""The masters' monthly backup to NUC8TB (Grey, 2026-10-07).

The nightly backup covers /home and /etc only; the music vault's copies were made by hand and
went stale. A month's copy is dated, linked to the last one, checked -- and only then are copies
beyond the newest two removed, never anything else on the drive.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from musaeus import music_backup as mb

pytestmark = pytest.mark.skipif(not shutil.which("rsync"), reason="requires rsync")


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "VAULT"
    for rel in (
        "Rock/America/Hearts/America - Sister Golden Hair.m4a",
        "Blues/SRV/Texas Flood/SRV - Pride And Joy.m4a",
    ):
        p = v / "Libraries" / "ALAC-Archival" / rel
        p.parent.mkdir(parents=True)
        p.write_bytes(b"audio" * 1000)
    (v / "MetaData").mkdir()
    (v / "MetaData" / "MasterLaw.csv").write_text("artist,genre\n")
    (v / "_db_backups").mkdir()
    for db in (v / "musaeus.db", v / "_db_backups" / "editions.db"):
        sqlite3.connect(db).execute("create table t(x)").connection.commit()
    return v


def test_a_first_copy_is_complete_and_checked(vault, tmp_path):
    root = tmp_path / "NUC"
    root.mkdir()
    r = mb.run(vault, root, today="20261101")
    assert r.problems == [] and r.files == 2 and r.removed == []
    assert (r.dest / "BACKUP_VERIFIED_AT.txt").is_file()
    assert (r.dest / "vault_state" / "MetaData" / "MasterLaw.csv").is_file()
    assert (r.dest / "vault_state" / "musaeus.db").is_file()


def test_an_unchanged_song_is_linked_not_copied_again(vault, tmp_path):
    root = tmp_path / "NUC"
    root.mkdir()
    first = mb.run(vault, root, today="20261101").dest
    second = mb.run(vault, root, today="20261201").dest
    rel = Path("Rock/America/Hearts/America - Sister Golden Hair.m4a")
    assert (first / "ALAC-Archival" / rel).stat().st_ino == (
        second / "ALAC-Archival" / rel
    ).stat().st_ino


def test_only_the_newest_two_copies_stay_and_nothing_else_is_touched(vault, tmp_path):
    root = tmp_path / "NUC"
    for keep in (
        "2.-MUSAEUS_legacy_314_20260926",
        "5.-MUSAEUS_pre_rebuild_2026-09-18",
        "3.-BACKUPS",
    ):
        (root / keep / "ALAC-Archival").mkdir(parents=True)
    mb.run(vault, root, today="20260924")
    mb.run(vault, root, today="20261006")
    r = mb.run(vault, root, today="20261101")
    names = sorted(p.name for p in root.iterdir())
    assert [p.name for p in r.removed] == ["2.-MUSAEUS_ALAC_Archive_20260924"]
    assert names == ["2.-MUSAEUS_ALAC_Archive_20261006", "2.-MUSAEUS_ALAC_Archive_20261101",
                     "2.-MUSAEUS_legacy_314_20260926", "3.-BACKUPS", "5.-MUSAEUS_pre_rebuild_2026-09-18"]  # fmt: skip


def test_a_copy_that_does_not_check_out_removes_nothing(vault, tmp_path, monkeypatch):
    root = tmp_path / "NUC"
    root.mkdir()
    mb.run(vault, root, today="20260924")
    mb.run(vault, root, today="20261006")
    monkeypatch.setattr(
        mb, "verify_copy", lambda v, d: (1, ["the copy differs from the masters (1 item(s))"])
    )
    r = mb.run(vault, root, today="20261101")
    assert r.problems and r.removed == []
    assert len(mb.dated_copies(root)) == 3
    assert not (r.dest / "BACKUP_VERIFIED_AT.txt").exists()


def test_a_damaged_copy_is_caught(vault, tmp_path):
    root = tmp_path / "NUC"
    root.mkdir()
    dest, _ = mb.make_copy(vault, root, "20261101")
    next((dest / "ALAC-Archival").rglob("*.m4a")).write_bytes(b"rot")
    files, problems = mb.verify_copy(vault, dest)
    assert any("differs" in p for p in problems)


def test_copies_are_found_newest_first(tmp_path):
    for d in (
        "2.-MUSAEUS_ALAC_Archive_20260924",
        "2.-MUSAEUS_ALAC_Archive_20261006",
        "2.-MUSAEUS_ALAC_Archive_bad",
    ):
        (tmp_path / d / "ALAC-Archival").mkdir(parents=True)
    assert [c.name for c in mb.dated_copies(tmp_path)] == [
        "2.-MUSAEUS_ALAC_Archive_20261006", "2.-MUSAEUS_ALAC_Archive_20260924"
    ]  # fmt: skip
