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
    # The failed copy is kept, as .partial: never counted, never removed by
    # rotation (review of #87, finding 3).
    assert len(mb.dated_copies(root)) == 2
    assert r.dest.name.endswith(mb.PARTIAL) and r.dest.is_dir()
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


# ── Codex audit 2026-10-16, F7 ────────────────────────────────────────────────
# A second run on the same day took the day's VERIFIED copy back under
# .partial and copied into it without removing what had left the masters. A
# master renamed in between left its old name behind, the counts never agreed
# again, every retry that day failed -- and the day's good copy was no longer
# counted as a copy at all.

SONG = Path("Rock/America/Hearts/America - Sister Golden Hair.m4a")


def _rename_a_master(vault):
    old = vault / "Libraries" / "ALAC-Archival" / SONG
    old.rename(old.with_name("America - Sister Golden Hair (Remastered).m4a"))


def test_a_same_day_retry_after_a_rename_succeeds(vault, tmp_path):
    root = tmp_path / "NUC"
    root.mkdir()
    mb.run(vault, root, today="20261101")
    _rename_a_master(vault)

    r = mb.run(vault, root, today="20261101")

    assert r.problems == [] and r.files == 2
    assert mb.dated_copies(root) == [root / f"{mb.PREFIX}20261101"]
    names = sorted(p.name for p in (r.dest / "ALAC-Archival").rglob("*.m4a"))
    assert "America - Sister Golden Hair (Remastered).m4a" in names
    assert "America - Sister Golden Hair.m4a" not in names
    assert [d.name for d in root.iterdir()] == [f"{mb.PREFIX}20261101"], "leftovers"


def test_a_failed_same_day_retry_keeps_the_days_verified_copy(vault, tmp_path, monkeypatch):
    root = tmp_path / "NUC"
    root.mkdir()
    first = mb.run(vault, root, today="20261101").dest
    monkeypatch.setattr(mb, "verify_copy", lambda vault, dest: (0, ["the copy differs"]))

    r = mb.run(vault, root, today="20261101")

    assert r.problems
    assert mb.dated_copies(root) == [first], "the day's verified copy stopped counting"
    assert (first / "BACKUP_VERIFIED_AT.txt").is_file()
    assert (first / "ALAC-Archival" / SONG).is_file()


def test_a_retry_after_a_failed_run_does_not_inherit_its_leftovers(vault, tmp_path, monkeypatch):
    root = tmp_path / "NUC"
    root.mkdir()
    real_verify = mb.verify_copy
    monkeypatch.setattr(mb, "verify_copy", lambda vault, dest: (0, ["the copy differs"]))
    mb.run(vault, root, today="20261101")  # leaves its .partial
    monkeypatch.setattr(mb, "verify_copy", real_verify)
    _rename_a_master(vault)

    r = mb.run(vault, root, today="20261101")

    assert r.problems == [] and r.files == 2


def test_a_swap_cut_short_is_put_right_on_the_next_run(vault, tmp_path, monkeypatch):
    # Killed between taking the old copy aside and naming the new one: the
    # verified old copy is the day's copy again, before anything else -- even
    # when the next run's own copy then fails its check.
    root = tmp_path / "NUC"
    root.mkdir()
    final = mb.run(vault, root, today="20261101").dest
    final.rename(final.with_name(final.name + mb.SUPERSEDED))
    assert mb.dated_copies(root) == []
    monkeypatch.setattr(mb, "verify_copy", lambda vault, dest: (0, ["the copy differs"]))

    r = mb.run(vault, root, today="20261101")

    assert r.problems and mb.dated_copies(root) == [final]


def test_a_swap_cut_short_on_an_earlier_day_is_put_right_too(vault, tmp_path):
    # Review of #200, finding 3: the next run is usually next month.
    root = tmp_path / "NUC"
    root.mkdir()
    old = mb.run(vault, root, today="20261001").dest
    old.rename(old.with_name(old.name + mb.SUPERSEDED))

    r = mb.run(vault, root, today="20261101")

    assert r.problems == [] and mb.dated_copies(root) == [r.dest, old]
    assert not list(root.glob(f"*{mb.SUPERSEDED}"))


# Review of #200, finding 1: masters moved or emptied "verified" against
# themselves -- an empty copy replaced the day's full one, and two empty
# months rotated every good copy away. On 2026-10-13 the masters WERE moved.


def _empty_the_masters(vault):
    for p in sorted((vault / "Libraries" / "ALAC-Archival").rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()


def test_an_empty_masters_folder_replaces_and_removes_nothing(vault, tmp_path):
    root = tmp_path / "NUC"
    root.mkdir()
    first = mb.run(vault, root, today="20261101").dest
    _empty_the_masters(vault)

    for day in ("20261101", "20261201", "20270101"):
        r = mb.run(vault, root, today=day)
        assert r.problems, day

    assert mb.dated_copies(root) == [first]
    assert (first / "ALAC-Archival" / SONG).is_file()


def test_masters_that_shrank_need_allow_shrink(vault, tmp_path):
    root = tmp_path / "NUC"
    root.mkdir()
    first = mb.run(vault, root, today="20261101").dest
    (vault / "Libraries" / "ALAC-Archival" / SONG).unlink()  # 2 -> 1 file

    r = mb.run(vault, root, today="20261201")
    assert r.problems and "allow-shrink" in r.problems[-1]
    assert mb.dated_copies(root) == [first]

    r = mb.run(vault, root, today="20261201", allow_shrink=True)
    assert r.problems == [] and r.files == 1


def test_a_second_run_on_the_same_drive_waits_its_turn(vault, tmp_path):
    # Review of #200, finding 4.
    import fcntl

    root = tmp_path / "NUC"
    root.mkdir()
    import os

    fd = os.open(root, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        r = mb.run(vault, root, today="20261101")
    finally:
        os.close(fd)
    assert r.problems == ["another backup run is using this drive"]
    assert mb.dated_copies(root) == []
