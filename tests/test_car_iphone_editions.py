"""Car and iPhone editions on the Lossless edition's framework (Grey, 2026-09-28).

Same plan, same record, same rules -- rows untouched, copies follow their
masters -- with the AAC encode, the Artist/Album layout, lossy masters
included, and (iPhone) a size budget.
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


def _master(cfg, rel: str, h: str, *, codec: str = "alac") -> Path:
    path = cfg.alac_archive / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    enc = ["-c:a", "aac"] if codec == "aac" else ["-c:a", "alac", "-sample_fmt", "s16p"]
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=660:duration=3",
         "-filter_complex", "[0]volume=-4dB[a];[1]volume=-8dB[b];[a][b]concat=n=2:v=0:a=1,pan=stereo|c0=c0|c1=c0",
         *enc, str(path)],
        check=True,
    )  # fmt: skip
    conn = open_db(cfg.db_path)
    upsert_archive(conn, {"file_path": str(path), "status": "CATALOGUED", "audio_hash": h,
                          "codec": codec, "size_bytes": path.stat().st_size})  # fmt: skip
    conn.commit()
    conn.close()
    return path


def _build(cfg, kind, **kw):
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    ledger = open_ledger(ledger_path(cfg))
    root = kind.root(cfg)
    try:
        plan = eb.make_plan(conn, ledger, cfg.alac_archive, root, kind=kind, **kw)
        out = eb.execute(plan, ledger, root, kind=kind, progress=lambda s: None,
                         wanted_csv=cfg.meta_dir / "TuneMyMusic.csv")  # fmt: skip
        return plan, out, copies(ledger, kind.name), copies(ledger, "lossless")
    finally:
        conn.close()
        ledger.close()


def test_car_copies_land_by_artist_and_album_as_aac_with_their_marker(cfg):
    _master(cfg, "Rock/Stones/Sticky Fingers/The Rolling Stones - Brown Sugar.m4a", "h1")
    _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h2", codec="aac")  # lossy
    before = sqlite3.connect(cfg.db_path).execute("SELECT * FROM archive").fetchall()
    plan, out, car, lossless = _build(cfg, eb.CAR_KIND)
    assert out.baked == 2 and not out.failed, out.failed
    copy = cfg.car_library / "Stones" / "Sticky Fingers" / "The Rolling Stones - Brown Sugar.m4a"
    assert car["h1"].output_path == str(copy) and copy.is_file()
    assert edition_bake.read_marker(copy) == eb.marker_for("h1", eb.CAR_KIND)
    assert edition_bake._audio_stream(edition_bake.probe(copy))["codec_name"] == "aac"
    assert "h2" in car, "the lossy master is in the car edition"
    assert lossless == {}, "the car build wrote Lossless records"
    assert sqlite3.connect(cfg.db_path).execute("SELECT * FROM archive").fetchall() == before
    assert not (cfg.meta_dir / "TuneMyMusic.csv").exists(), (
        "car copies do not go on the wanted list"
    )
    plan, out, car, _ = _build(cfg, eb.CAR_KIND)
    assert plan.up_to_date == 2 and not plan.bake


def test_two_masters_that_meet_in_the_car_layout_are_caught(cfg):
    # The car layout drops the genre folder.
    _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    _master(cfg, "Soft Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h2")
    plan, out, car, _ = _build(cfg, eb.CAR_KIND)
    assert out.baked == 1 and len(plan.blocked) == 1
    assert "same place" in plan.blocked[0][1]


def test_the_iphone_budget_adds_and_drops_tracks(cfg):
    a = _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    b = _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Wild Horses.m4a", "h2")
    plan, out, phone, _ = _build(cfg, eb.IPHONE_KIND, allowed={str(a), str(b)})
    assert out.baked == 2
    plan, out, phone, _ = _build(cfg, eb.IPHONE_KIND, allowed={str(a)})  # budget shrank
    assert out.removed == 1 and set(phone) == {"h1"}
    assert len(plan.over_budget) == 1


def test_a_budgeted_track_that_is_only_blocked_keeps_its_iphone_copy(cfg):
    # Cloud review of #53: with a budget, every live master not selected was
    # taken for "dropped for space" -- a master blocked for another reason
    # (here: found not to decode) lost its copy while still in the budget.
    a = _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    b = _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Wild Horses.m4a", "h2")
    _build(cfg, eb.IPHONE_KIND, allowed={str(a), str(b)})
    conn = sqlite3.connect(cfg.db_path)
    if "decode_ok" not in {r[1] for r in conn.execute("PRAGMA table_info(archive)")}:
        conn.execute(
            "ALTER TABLE archive ADD COLUMN decode_ok INTEGER"
        )  # the live catalogue has it
    conn.execute("UPDATE archive SET decode_ok = 0 WHERE audio_hash = 'h2'")
    conn.commit()
    conn.close()
    plan, out, phone, _ = _build(cfg, eb.IPHONE_KIND, allowed={str(a), str(b)})
    assert len(plan.blocked) == 1
    assert out.removed == 0 and set(phone) == {"h1", "h2"}, "a blocked track lost its copy"


def test_the_car_estimate_uses_the_aac_encodes_own_rate():
    # The AAC encode measured 0.18 worker-s per audio second; the Lossless
    # figure (0.04, scaled by rate) would have promised the car in a quarter
    # of its time.
    m = eb.Master(Path("m.m4a"), "h", "alac", -20.0, -3.0, 1, 1, seconds=3600.0, seconds_44k=3600.0)
    car = eb.Plan(bake=[(m, Path("c.m4a"))], kind_name="car")
    lossless = eb.Plan(bake=[(m, Path("c.m4a"))], kind_name=eb.EDITION)
    assert car.hours(1) == pytest.approx(eb.AAC_WORK_PER_AUDIO_SECOND)
    assert lossless.hours(1) == pytest.approx(eb.WORK_PER_AUDIO_SECOND)


def test_playlists_in_the_car_folder_are_not_unknown_files(cfg):
    # Cloud review of #53: the playlist stage can write its index into
    # CAR_Library/Playlists, and every car build listed those as unknown
    # files. A real stray song is still listed.
    _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    (cfg.car_library / "Playlists").mkdir(parents=True)
    (cfg.car_library / "Playlists" / "Rock.m3u8").write_text("#EXTM3U\n")
    stray = cfg.car_library / "Nobody" / "Nothing" / "Nobody - Stray.m4a"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"\0")
    plan, _, _, _ = _build(cfg, eb.CAR_KIND)
    assert plan.unrecorded == [stray]
