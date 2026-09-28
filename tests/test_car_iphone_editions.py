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

from musaeus import deep_scan, edition_bake
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
    deep_scan.ensure_columns(conn)  # the live catalogue has decode_ok
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


def test_the_budget_is_filled_only_with_tracks_the_build_can_make(cfg):
    # Cloud review of #53: the budget was filled from every catalogued row,
    # so a track the build cannot make (here: it does not decode) took the
    # space and the phone came out short. The budget here fits one track.
    from musaeus.editions import IPHONE, estimated_bytes, load_tracks

    _master(cfg, "Rock/Aa/Al/Aa - First.m4a", "h1")  # first in the order, blocked
    _master(cfg, "Rock/Zz/Al/Zz - Last.m4a", "h2")
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    deep_scan.ensure_columns(conn)  # the live catalogue has decode_ok
    conn.execute("UPDATE archive SET decode_ok = 0 WHERE audio_hash = 'h1'")
    conn.commit()
    one = max(estimated_bytes(t, IPHONE) for t in load_tracks(conn))
    conn.close()
    r = _cli(cfg, "iphone", "--budget-gb", f"{1.5 * one / 1e9:.9f}", "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "To bake       : 1 track(s)" in r.stdout, r.stdout


def _cli(cfg, *args: str) -> subprocess.CompletedProcess[str]:
    """`musaeus edition-build ...` against this test's vault."""
    import os
    import sys

    env = {**os.environ, "MUSAEUS_VAULT_ROOT": str(cfg.vault_root),
           "MUSAEUS_DB_PATH": str(cfg.db_path)}  # fmt: skip
    return subprocess.run(
        [sys.executable, "-m", "musaeus.cli", "edition-build", *args],
        capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
        cwd=Path(__file__).resolve().parents[1], timeout=120,
    )  # fmt: skip


@pytest.mark.parametrize(
    "args,why",
    [
        (("iphone", "--budget-gb", "0"), "more than 0"),
        (("car", "--budget-gb", "30"), "iphone"),
        (("lossless", "--budget-gb", "30"), "iphone"),
        (("car", "--lossy", "alac"), "lossless"),
        (("iphone", "--lossy", "leave-out"), "lossless"),
    ],
)
def test_an_option_that_would_do_nothing_is_refused(cfg, args, why):
    # Cloud review of #53: --budget-gb 0 counted as "no budget" (the whole
    # library into the phone), and --budget-gb / --lossy on an edition that
    # has no use for them were silently ignored.
    _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    r = _cli(cfg, *args, "--dry-run")
    assert r.returncode != 0 and why in r.stderr, r.stdout + r.stderr


def test_the_build_and_the_preview_place_a_track_by_one_rule(tmp_path):
    # Cloud review of #53: the car layout was written twice (the build's and
    # the selection preview's). Both now call editions.artist_album_path.
    from musaeus.editions import CAR, Track, output_path_for

    for rel in ("Rock/Stones/Hits/Angie.m4a", "Angie.flac", "Stones/Angie.m4a"):
        master = tmp_path / "masters" / rel
        t = Track(str(master), "", "", "", "", 1.0, 1)
        assert eb.CAR_KIND.place(tmp_path / "masters", tmp_path / "car", master) == (
            output_path_for(t, CAR, tmp_path / "car")
        )


def test_only_the_edition_kinds_name_the_edition_folders():
    # Cloud review of #53: the edition -> folder table was written by hand in
    # the playlist stage, the audit and the CLI. Kind.root_attr is the one.
    root = Path(__file__).resolve().parents[1]
    home = {root / "musaeus" / "config.py", root / "musaeus" / "edition_build.py"}
    offenders = [
        str(p.relative_to(root))
        for p in [*(root / "musaeus").rglob("*.py"), *(root / "scripts").rglob("*.py")]
        if p not in home
        and ('"car_library"' in p.read_text() or '"iphone_library"' in p.read_text())
    ]
    assert offenders == [], offenders


def test_each_aac_edition_is_sized_by_its_own_format(monkeypatch):
    # Cloud review of #53: the iPhone plan was sized with the car's spec --
    # right only while both say 256k.
    from dataclasses import replace

    from musaeus import editions

    monkeypatch.setitem(editions.EDITIONS, "iphone", replace(editions.IPHONE, bitrate_kbps=128))
    m = eb.Master(Path("m.m4a"), "h", "alac", -20.0, -3.0, 1, 1, seconds=600.0)
    car = eb.Plan(bake=[(m, Path("c.m4a"))], kind_name="car")
    phone = eb.Plan(bake=[(m, Path("p.m4a"))], kind_name="iphone")
    assert phone.bake_bytes < car.bake_bytes


def _count_measures(monkeypatch) -> list[Path]:
    seen: list[Path] = []
    real = edition_bake.ffmpeg_measure_loudnorm

    def counting(path, *a, **k):
        seen.append(Path(path))
        return real(path, *a, **k)

    monkeypatch.setattr(edition_bake, "ffmpeg_measure_loudnorm", counting)
    return seen


def test_a_song_is_measured_once_for_the_car_and_the_iphone(cfg, monkeypatch):
    # Grey, 2026-09-28: keep each song's measurement in the build's record,
    # so the iPhone build (same -14 target, same filters) and any rebuild
    # skip it. The measure pass is about half of every song's time.
    _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    measures = _count_measures(monkeypatch)
    _, car_out, _, _ = _build(cfg, eb.CAR_KIND)
    assert car_out.baked == 1 and len(measures) == 1
    _, phone_out, phone, _ = _build(cfg, eb.IPHONE_KIND)
    assert phone_out.baked == 1 and not phone_out.failed, phone_out.failed
    assert len(measures) == 1, "the iPhone build measured the song again"
    assert phone["h1"].achieved_lufs is not None


def test_a_measurement_serves_only_its_own_recipe(cfg, monkeypatch):
    # The Lossless edition measures for -18 at the master's own rate; the car
    # for -14 after its filters. Neither may stand in for the other.
    _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    measures = _count_measures(monkeypatch)
    _build(cfg, eb.LOSSLESS_KIND)
    _build(cfg, eb.CAR_KIND)
    assert len(measures) == 2


def test_the_estimate_knows_which_songs_are_already_measured(cfg):
    _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    car_plan, _, _, _ = _build(cfg, eb.CAR_KIND)
    phone_plan, _, _, _ = _build(cfg, eb.IPHONE_KIND)
    assert phone_plan.hours(1) == pytest.approx(car_plan.hours(1) * (1 - eb.AAC_MEASURE_SHARE))
