"""doctor: an edition copy that lost a tag its master still has.

On 2026-11-24 a hand script removed trkn, disk and ©cmt from every copy in
all three editions. The ledger calls a copy current while the MASTER's mtime
is unchanged, so the next build does not restore them and nothing said so.

Measured 2026-11-25 on the real vault: 8,341 of 18,450 recorded copies are
missing at least one of the three (track 3,250, comment 5,091, disc 21).

The drift test fails on 6cad046 for the right reason: no finding with this
name exists at all.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from musaeus import doctor
from musaeus.edition_ledger import Copy

CHECK = "edition copies keep master tags"


@pytest.fixture
def cfg(tmp_path: Path) -> SimpleNamespace:
    lib = tmp_path / "ALAC-Library"
    lib.mkdir()
    db = tmp_path / "musaeus.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE archive (file_path TEXT, artist TEXT, title TEXT, status TEXT, "
        "audio_hash TEXT, genre TEXT, finalized_at TEXT, duration REAL)"
    )
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts TEXT, "
        "event_type TEXT, file_path TEXT, old_value TEXT, new_value TEXT, stage TEXT, note TEXT)"
    )
    conn.execute(
        "CREATE TABLE duplicates (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT, "
        "file_path TEXT, status TEXT, audio_hash TEXT)"
    )
    conn.commit()
    conn.close()
    idx = tmp_path / "hash_index.db"
    ic = sqlite3.connect(idx)
    ic.execute("CREATE TABLE finalized_hashes (audio_hash TEXT, file_path TEXT)")
    ic.commit()
    ic.close()
    return SimpleNamespace(
        db_path=db,
        alac_library=lib,
        alac_archive=tmp_path / "ALAC-Archival",
        libraries=tmp_path,
        hash_index_path=idx,
        vault_root=tmp_path,
        db_history_dir=tmp_path / "MetaData",
        meta_dir=tmp_path / "MetaData",
    )


def _copy(edition: str, master: Path, output: Path) -> Copy:
    return Copy(
        edition=edition,
        master_hash="h",
        master_path=str(master),
        master_mtime_ns=None,
        output_path=str(output),
        built_at="2026-11-24T00:00:00",
        achieved_lufs=None,
        mode="copy",
    )


def _finding(rep):
    return next((f for f in rep.findings if f.check == CHECK), None)


def test_a_copy_that_lost_a_master_tag_is_reported(cfg, monkeypatch):
    master = cfg.vault_root / "Artist" / "Album" / "Artist - Song.m4a"
    copy = cfg.alac_library / "Artist" / "Album" / "Artist - Song.m4a"
    for p in (master, copy):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")

    tags = {str(master): {"trkn": [(3, 12)], "\xa9cmt": ["keep me"]}, str(copy): {}}
    monkeypatch.setattr(doctor, "_every_edition_copy", lambda cfg: [_copy("car", master, copy)])
    monkeypatch.setattr(doctor, "_mp4_tags", lambda p: tags[str(p)], raising=False)

    finding = _finding(doctor.diagnose(cfg))

    assert finding is not None, "doctor said nothing about the lost tags"
    assert finding.level == "warn"
    assert finding.count == 1
    assert "track 1" in finding.detail
    assert "comment 1" in finding.detail
    assert "Artist - Song.m4a lost its track" in finding.detail


def test_matching_tags_are_ok(cfg, monkeypatch):
    master = cfg.vault_root / "A" / "B" / "s.m4a"
    copy = cfg.alac_library / "A" / "B" / "s.m4a"
    for p in (master, copy):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")

    same = {"trkn": [(1, 2)], "disk": [(1, 1)], "\xa9cmt": ["x"]}
    monkeypatch.setattr(doctor, "_every_edition_copy", lambda cfg: [_copy("car", master, copy)])
    monkeypatch.setattr(doctor, "_mp4_tags", lambda p: dict(same), raising=False)

    finding = _finding(doctor.diagnose(cfg))

    assert finding is not None
    assert finding.level == "ok"
    assert "1 recorded copy/copies" in finding.detail


def test_a_tag_the_master_never_had_is_not_a_loss(cfg, monkeypatch):
    master = cfg.vault_root / "A" / "B" / "s.m4a"
    copy = cfg.alac_library / "A" / "B" / "s.m4a"
    for p in (master, copy):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")

    tags = {str(master): {}, str(copy): {"\xa9cmt": ["only the copy has this"]}}
    monkeypatch.setattr(doctor, "_every_edition_copy", lambda cfg: [_copy("car", master, copy)])
    monkeypatch.setattr(doctor, "_mp4_tags", lambda p: tags[str(p)], raising=False)

    finding = _finding(doctor.diagnose(cfg))

    assert finding is not None
    assert finding.level == "ok"


def test_a_copy_whose_master_is_gone_is_left_to_the_other_check(cfg, monkeypatch):
    gone = cfg.vault_root / "A" / "B" / "gone.m4a"
    copy = cfg.alac_library / "A" / "B" / "gone.m4a"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(b"x")

    monkeypatch.setattr(doctor, "_every_edition_copy", lambda cfg: [_copy("car", gone, copy)])
    monkeypatch.setattr(doctor, "_mp4_tags", lambda p: {"trkn": [(1, 1)]}, raising=False)

    finding = _finding(doctor.diagnose(cfg))

    assert finding is not None
    assert finding.level == "ok"


def test_an_unreadable_file_is_not_the_same_as_an_empty_tag_set(tmp_path):
    """{} means "read, and has no tags". An unreadable file is not that.

    Returning {} for both made every populated master tag look lost on a copy
    that could not be read at all (CodeRabbit on #338, 2026-11-27). On the real
    library that is a false alarm per unreadable file, and it looks exactly
    like the 2026-11-24 damage.
    """
    bad = tmp_path / "not-really-audio.m4a"
    bad.write_bytes(b"this is not an mp4")

    assert doctor._mp4_tags(bad) is None, "unreadable must be distinguishable from empty"


def test_an_unreadable_copy_is_not_reported_as_having_lost_its_tags(cfg, monkeypatch):
    """A copy that cannot be read is not evidence that its tags were stripped."""
    master = cfg.vault_root / "A" / "B" / "s.m4a"
    copy = cfg.alac_library / "A" / "B" / "s.m4a"
    for p in (master, copy):
        p.parent.mkdir(parents=True, exist_ok=True)
    master.write_bytes(b"x")
    copy.write_bytes(b"this is not an mp4")

    real = doctor._mp4_tags
    monkeypatch.setattr(
        doctor, "_mp4_tags", lambda p: {"trkn": [(3, 12)]} if Path(p) == master else real(p)
    )
    monkeypatch.setattr(doctor, "_every_edition_copy", lambda cfg: [_copy("car", master, copy)])

    finding = _finding(doctor.diagnose(cfg))

    assert finding is not None
    assert finding.level == "ok", f"an unreadable copy was called damaged: {finding.detail}"
    assert "unreadable" in finding.detail
    assert "lost its track" not in finding.detail


def test_a_copy_that_was_never_built_is_not_counted_as_checked(cfg, monkeypatch):
    """A missing output was skipped from the comparison but still counted.

    The success line then claimed a copy that does not exist "carries every
    tag its master has" (CodeRabbit on #338, 2026-11-27).
    """
    master = cfg.vault_root / "A" / "B" / "s.m4a"
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_bytes(b"x")
    never_built = cfg.alac_library / "A" / "B" / "s.m4a"

    monkeypatch.setattr(doctor, "_mp4_tags", lambda p: {"trkn": [(1, 1)]}, raising=False)
    monkeypatch.setattr(
        doctor, "_every_edition_copy", lambda cfg: [_copy("car", master, never_built)]
    )

    finding = _finding(doctor.diagnose(cfg))

    assert finding is not None
    assert finding.level == "ok"
    assert finding.count == 0, (
        f"a copy that was never built was counted as checked: {finding.count}"
    )
    assert "not yet built" in finding.detail
