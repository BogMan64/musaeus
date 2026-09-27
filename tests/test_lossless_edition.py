"""The Lossless edition build (musaeus/edition_build.py).

Real ffmpeg on short synthetic masters: the build's whole job is files on
disk, so the tests look at files on disk. Skipped when ffmpeg is absent.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from musaeus import edition_bake
from musaeus import edition_build as eb
from musaeus.config import MusicConfig
from musaeus.db import open_db, upsert_archive
from musaeus.edition_ledger import copies, ledger_path, open_ledger

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not available",
)

REL = Path("Rock") / "Stones" / "Sticky Fingers" / "The Rolling Stones - Brown Sugar.m4a"


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
    c = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        db_path=tmp_path / "musaeus.db",
    )
    c.ensure_dirs()
    return c


def _master(cfg, rel: Path, h: str, *, codec: str = "alac", lufs: float = -12.0) -> Path:
    path = cfg.alac_archive / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    enc = ["-c:a", "alac", "-sample_fmt", "s16p"] if codec == "alac" else ["-c:a", "aac"]
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=6", "-af", "volume=-6dB", *enc, str(path)],
        check=True,
    )  # fmt: skip
    conn = open_db(cfg.db_path)
    upsert_archive(
        conn,
        {
            "file_path": str(path),
            "status": "CATALOGUED",
            "audio_hash": h,
            "codec": codec,
            "size_bytes": path.stat().st_size,
        },
    )
    conn.execute("UPDATE archive SET lufs = ? WHERE file_path = ?", (lufs, str(path)))
    conn.commit()
    conn.close()
    return path


def _catalogue(cfg) -> list[tuple]:
    conn = sqlite3.connect(cfg.db_path)
    rows = conn.execute("SELECT * FROM archive ORDER BY id").fetchall()
    conn.close()
    return rows


def _build(cfg, **kw):
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    ledger = open_ledger(ledger_path(cfg))
    try:
        plan = eb.make_plan(conn, ledger, cfg.alac_archive, cfg.alac_library, **kw)
        out = eb.execute(plan, ledger, cfg.alac_library, workers=2, progress=lambda s: None)
        return plan, out, copies(ledger, eb.EDITION)
    finally:
        conn.close()
        ledger.close()


def test_a_build_bakes_every_master_and_changes_no_row(cfg):
    _master(cfg, REL, "h1")
    other = Path("Rock") / "Stones" / "Hits" / "The Rolling Stones - Angie.m4a"
    _master(cfg, other, "h2")
    before = _catalogue(cfg)
    plan, out, recorded = _build(cfg)
    assert out.baked == 2 and not out.failed, out.failed
    for rel, h in ((REL, "h1"), (other, "h2")):
        copy = cfg.alac_library / rel
        assert copy.is_file()
        assert edition_bake.read_marker(copy) == eb.marker_for(h)
        assert recorded[h].output_path == str(copy)
    assert _catalogue(cfg) == before, "building an edition changed the catalogue"


def test_a_second_build_does_nothing(cfg):
    _master(cfg, REL, "h1")
    _build(cfg)
    plan, out, _ = _build(cfg)
    assert plan.up_to_date == 1 and not plan.bake and out.baked == 0


def test_a_moved_master_moves_its_copy_without_baking_again(cfg):
    master = _master(cfg, REL, "h1")
    _build(cfg)
    new_rel = Path("Rock") / "Rolling Stones" / "Sticky Fingers" / REL.name
    new_master = cfg.alac_archive / new_rel
    new_master.parent.mkdir(parents=True, exist_ok=True)
    master.rename(new_master)
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("UPDATE archive SET file_path = ?", (str(new_master),))
    conn.commit()
    conn.close()
    plan, out, recorded = _build(cfg)
    assert out.moved == 1 and out.baked == 0
    assert (cfg.alac_library / new_rel).is_file() and not (cfg.alac_library / REL).exists()
    assert recorded["h1"].output_path == str(cfg.alac_library / new_rel)


def test_a_retagged_master_retags_its_copy_without_baking_again(cfg):
    from mutagen.mp4 import MP4

    master = _master(cfg, REL, "h1")
    _build(cfg)
    f = MP4(master)
    f.tags["\xa9nam"] = ["Brown Sugar (2009 Remaster)"]
    f.save()
    plan, out, _ = _build(cfg)
    assert out.retagged == 1 and out.baked == 0
    assert MP4(cfg.alac_library / REL).tags["\xa9nam"] == ["Brown Sugar (2009 Remaster)"]


def test_a_master_that_left_the_library_takes_its_copy_with_it(cfg):
    _master(cfg, REL, "h1")
    _build(cfg)
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("UPDATE archive SET status = 'DUPE_REVIEW'")
    conn.commit()
    conn.close()
    plan, out, recorded = _build(cfg)
    assert out.removed == 1
    assert not (cfg.alac_library / REL).exists()
    assert "h1" not in recorded


def test_a_file_with_no_record_is_never_deleted_or_overwritten(cfg):
    _master(cfg, REL, "h1")
    in_the_way = cfg.alac_library / REL
    in_the_way.parent.mkdir(parents=True, exist_ok=True)
    in_the_way.write_bytes(b"someone else's file")
    stranger = cfg.alac_library / "Stranger.m4a"
    stranger.write_bytes(b"x")
    plan, out, recorded = _build(cfg)
    assert in_the_way.read_bytes() == b"someone else's file"
    assert stranger.is_file()
    assert plan.unrecorded == [stranger]
    assert [why for _, why in plan.blocked] and "h1" not in recorded


def test_a_copy_renamed_into_place_but_not_recorded_is_adopted(cfg):
    master = _master(cfg, REL, "h1")
    target = cfg.alac_library / REL
    target.parent.mkdir(parents=True, exist_ok=True)
    edition_bake.bake(master, target)
    edition_bake.copy_tags(master, target, eb.marker_for("h1"))  # the build stopped here
    plan, out, recorded = _build(cfg)
    assert out.adopted == 1 and out.baked == 0
    assert recorded["h1"].output_path == str(target)


def test_lossy_masters_are_left_out_unless_asked_for(cfg):
    _master(cfg, REL, "h1", codec="aac")
    plan, out, _ = _build(cfg)
    assert len(plan.lossy_left_out) == 1 and out.baked == 0
    plan, out, _ = _build(cfg, include_lossy=True)
    assert out.baked == 1


def test_a_failed_bake_leaves_nothing_behind(cfg, monkeypatch):
    _master(cfg, REL, "h1")

    def broken(source, tmp):
        tmp.write_bytes(b"half a file")
        raise edition_bake.BakeError("baked to -9.00 LUFS, wanted -18.0")

    monkeypatch.setattr(edition_bake, "bake", broken)
    plan, out, recorded = _build(cfg)
    assert out.failed and out.baked == 0 and not recorded
    assert not list(cfg.alac_library.rglob("*.m4a*")), "a failed bake left a file behind"


def test_the_quiet_masters_that_would_be_compressed_are_counted():
    loud = eb.Master(Path("a"), "h", "alac", lufs=-10.0, lufs_tp=-0.5, size_bytes=1, mtime_ns=1)
    quiet_peaky = eb.Master(
        Path("b"), "h", "alac", lufs=-22.0, lufs_tp=-2.0, size_bytes=1, mtime_ns=1
    )
    quiet_room = eb.Master(
        Path("c"), "h", "alac", lufs=-20.0, lufs_tp=-6.0, size_bytes=1, mtime_ns=1
    )
    quiet_unknown = eb.Master(
        Path("d"), "h", "alac", lufs=-21.0, lufs_tp=None, size_bytes=1, mtime_ns=1
    )
    assert loud.may_compress() is False
    assert quiet_peaky.may_compress() is True  # +4 dB puts a -2 dBTP peak at +2
    assert quiet_room.may_compress() is False  # +2 dB leaves a -6 dBTP peak at -4
    assert quiet_unknown.may_compress() is None
