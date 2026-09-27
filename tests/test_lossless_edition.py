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


def _master(
    cfg, rel: Path, h: str, *, codec: str | None = "alac", lufs: float = -12.0, seconds: int = 6
) -> Path:
    path = cfg.alac_archive / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    enc = ["-c:a", "aac"] if codec == "aac" else ["-c:a", "alac", "-sample_fmt", "s16p"]
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}", "-af", "volume=-6dB", *enc, str(path)],
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


def _build(cfg, workers=2, **kw):
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    ledger = open_ledger(ledger_path(cfg))
    try:
        plan = eb.make_plan(conn, ledger, cfg.alac_archive, cfg.alac_library, **kw)
        out = eb.execute(plan, ledger, cfg.alac_library, workers=workers, progress=lambda s: None)
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
    # True is a certainty; linear is never promised -- loudnorm also goes
    # dynamic on a wide loudness range, which is not stored (cloud review).
    assert loud.may_compress() is None
    assert quiet_peaky.may_compress() is True  # +4 dB puts a -2 dBTP peak at +2
    assert quiet_room.may_compress() is None  # its peak allows it; its range may not
    assert quiet_unknown.may_compress() is None


def test_a_new_master_on_a_freed_path_replaces_the_old_copy_in_one_build(cfg):
    # Every baked-copy swap ends this way: the original is renamed into the
    # plain name the baked copy left, so the master at that path is a NEW
    # recording while the old copy still sits at the target, due for removal.
    master = _master(cfg, REL, "old")
    _build(cfg)
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("UPDATE archive SET audio_hash = 'new'")  # the original took the path
    conn.commit()
    conn.close()
    master.touch()
    plan, out, recorded = _build(cfg)
    assert not plan.blocked, plan.blocked
    assert out.removed == 1 and out.baked == 1
    assert set(recorded) == {"new"}
    assert edition_bake.read_marker(cfg.alac_library / REL) == eb.marker_for("new")


# ── Cloud review of #49 (2026-09-27): each reproduced there by running ──


def _sql(cfg, statement, *args):
    conn = sqlite3.connect(cfg.db_path)
    conn.execute(statement, args)
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    "why",
    ["catalogue reset", "fingerprint cleared", "row points at a renamed file", "left out as lossy"],
)
def test_review_a_copy_is_removed_only_when_its_master_is_gone(cfg, why):
    # A copy was removed for every recorded master not SELECTED this build.
    # After a reset (musaeus.db is wiped between batches) that was every copy.
    master = _master(cfg, REL, "h1")
    _build(cfg)
    if why == "catalogue reset":
        _sql(cfg, "DELETE FROM archive")
    elif why == "fingerprint cleared":
        _sql(cfg, "UPDATE archive SET audio_hash = NULL")
    elif why == "row points at a renamed file":
        master.rename(master.with_name("renamed by hand.m4a"))
        _sql(cfg, "UPDATE archive SET audio_hash = audio_hash")  # row still names the old path
    else:
        _sql(cfg, "UPDATE archive SET codec = 'aac'")
    plan, out, recorded = _build(cfg)
    assert out.removed == 0 and (cfg.alac_library / REL).is_file(), why
    assert "h1" in recorded


def test_review_a_copy_goes_when_its_master_was_set_aside(cfg):
    _master(cfg, REL, "h1")
    _build(cfg)
    _sql(cfg, "UPDATE archive SET status = 'DUPE_REVIEW'")
    plan, out, recorded = _build(cfg)
    assert out.removed == 1 and not (cfg.alac_library / REL).exists()


def test_review_a_record_whose_file_is_another_copy_is_not_moved_or_trusted(cfg):
    # X was recorded at A; A now holds Y's copy (a stopped build). X's master
    # moved to Z. The move step renamed Y's file to Z and stamped X's marker
    # on it: a 9 s copy passing for a 6 s master, "up to date" for good.
    a = REL
    z = Path("Rock") / "Stones" / "Other" / REL.name
    x = _master(cfg, a, "hx", seconds=6)
    _build(cfg)
    (cfg.alac_archive / z).parent.mkdir(parents=True, exist_ok=True)
    x.rename(cfg.alac_archive / z)
    _sql(cfg, "UPDATE archive SET file_path = ?", str(cfg.alac_archive / z))
    y = _master(cfg, a, "hy", seconds=9)
    target = cfg.alac_library / a
    target.unlink()
    edition_bake.bake(y, target)
    edition_bake.copy_tags(y, target, eb.marker_for("hy"))
    plan, out, recorded = _build(cfg)
    assert not out.failed, out.failed
    assert edition_bake.read_marker(cfg.alac_library / z) == eb.marker_for("hx")
    assert edition_bake.read_marker(target) == eb.marker_for("hy")
    from musaeus.duration import stream_seconds

    dz = stream_seconds(cfg.alac_library / z)
    assert dz is not None and abs(dz - 6.0) < 0.5, "Y's audio passed off as X's copy"
    assert recorded["hx"].output_path == str(cfg.alac_library / z)
    assert recorded["hy"].output_path == str(target)


def test_review_a_move_stopped_after_the_rename_is_adopted_not_rebaked(cfg):
    master = _master(cfg, REL, "h1")
    _build(cfg)
    new_rel = Path("Rock") / "Rolling Stones" / "SF" / REL.name
    (cfg.alac_archive / new_rel).parent.mkdir(parents=True, exist_ok=True)
    master.rename(cfg.alac_archive / new_rel)
    _sql(cfg, "UPDATE archive SET file_path = ?", str(cfg.alac_archive / new_rel))
    (cfg.alac_library / new_rel).parent.mkdir(parents=True, exist_ok=True)
    (cfg.alac_library / REL).rename(cfg.alac_library / new_rel)  # stopped before record()
    plan, out, recorded = _build(cfg)
    assert out.adopted == 1 and out.baked == 0 and not out.failed, out.failed
    assert recorded["h1"].output_path == str(cfg.alac_library / new_rel)


def test_review_a_swap_of_two_copies_finishes_in_one_build(cfg):
    one, two = Path("Rock") / "A" / "1.m4a", Path("Rock") / "A" / "2.m4a"
    a, b = _master(cfg, one, "ha"), _master(cfg, two, "hb")
    _build(cfg)
    tmp = a.with_name("swap.m4a")
    a.rename(tmp)
    b.rename(a)
    tmp.rename(b)
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("UPDATE archive SET file_path = 'swapping' WHERE audio_hash = 'ha'")
    conn.execute("UPDATE archive SET file_path = ? WHERE audio_hash = 'hb'", (str(a),))
    conn.execute("UPDATE archive SET file_path = ? WHERE audio_hash = 'ha'", (str(b),))
    conn.commit()
    conn.close()
    plan, out, recorded = _build(cfg)
    assert out.moved == 2 and not out.failed, out.failed
    assert edition_bake.read_marker(cfg.alac_library / two) == eb.marker_for("ha")
    assert edition_bake.read_marker(cfg.alac_library / one) == eb.marker_for("hb")


def test_review_a_master_that_does_not_decode_is_not_baked(cfg):
    master = _master(cfg, REL, "h1", seconds=20)
    data = bytearray(master.read_bytes())
    mid = len(data) // 2
    data[mid : mid + 20_000] = bytes((i * 37) % 256 for i in range(20_000))
    master.write_bytes(bytes(data))
    plan, out, recorded = _build(cfg)
    assert out.baked == 0 and not recorded
    assert any("decode" in why for _, why in out.failed), out.failed


def test_review_a_master_with_no_codec_recorded_is_not_taken_for_lossy(cfg):
    _master(cfg, REL, "h1")
    _sql(cfg, "UPDATE archive SET codec = NULL")
    plan, out, recorded = _build(cfg)
    assert not plan.lossy_left_out and out.baked == 1


def test_review_a_record_on_a_path_now_holding_a_new_copy_does_not_stop_the_build(cfg):
    # The reviewer's order: X recorded at A, its copy gone, X's master moved
    # to Z, a new master Y at A. Y's copy is recorded at A while X's record
    # still names A: UNIQUE (edition, output_path) raised outside the
    # per-track guard, and the pool kept baking after the lock was released.
    a = REL
    z = Path("Rock") / "Stones" / "Zz" / REL.name  # sorts after A: Y is baked first
    x = _master(cfg, a, "hx")
    _build(cfg)
    (cfg.alac_library / a).unlink()
    (cfg.alac_archive / z).parent.mkdir(parents=True, exist_ok=True)
    x.rename(cfg.alac_archive / z)
    _sql(cfg, "UPDATE archive SET file_path = ?", str(cfg.alac_archive / z))
    _master(cfg, a, "hy", seconds=9)
    plan, out, recorded = _build(cfg, workers=1)
    assert out.baked == 2 and not out.failed, out.failed
    assert recorded["hy"].output_path == str(cfg.alac_library / a)
    assert recorded["hx"].output_path == str(cfg.alac_library / z)
