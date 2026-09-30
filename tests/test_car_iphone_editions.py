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
    not (shutil.which("ffmpeg") and shutil.which("ffprobe") and shutil.which("fdkaac")),
    reason="ffmpeg/ffprobe/fdkaac not available",
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
           "MUSAEUS_DB_PATH": str(cfg.db_path),
           "MUSAEUS_NO_SLEEP_INHIBIT": "1", "MUSAEUS_NO_IDLE_THROTTLE": "1"}  # fmt: skip
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
        # At range 11 compression is normal in the car: the flag would re-make
        # about 7 in 10 copies for nothing (cloud review of #53).
        (("car", "--rebake-compressed"), "lossless"),
        (("iphone", "--rebake-compressed"), "lossless"),
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


def test_the_budget_never_goes_to_a_track_the_build_then_blocks(cfg):
    # Cloud review of #53: makeable was decided before the second pass could
    # still block a master (a file with no record in its place). The budget
    # went to that one, the buildable track was put over budget, and its
    # iPhone copy was deleted -- an empty edition, exit 0.
    from musaeus.editions import IPHONE, estimated_bytes, load_tracks

    _master(cfg, "Rock/Aa/Al/Aa - First.m4a", "h1")  # first in the fill order
    zz = _master(cfg, "Rock/Zz/Al/Zz - Last.m4a", "h2")
    _build(cfg, eb.IPHONE_KIND, allowed={str(zz)})  # Zz is on the phone
    stranger = cfg.iphone_library / "Aa" / "Al" / "Aa - First.m4a"
    stranger.parent.mkdir(parents=True)
    stranger.write_bytes(b"\0")  # an unmarked file where Aa's copy would go
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    one = max(estimated_bytes(t, IPHONE) for t in load_tracks(conn))
    ledger = open_ledger(ledger_path(cfg))
    try:
        plan, _ = eb.budgeted(
            conn, ledger, cfg.alac_archive, cfg.iphone_library, eb.IPHONE_KIND, int(1.5 * one)
        )
    finally:
        conn.close()
        ledger.close()
    assert not plan.remove, "the buildable track's copy was dropped for a blocked one"
    assert plan.up_to_date == 1


def test_the_place_goes_to_the_master_whose_copy_is_there(cfg):
    # Cloud review of #53: two masters meeting at one Artist/Album/Title --
    # the place went to whichever sorted first, so a newcomer in an earlier
    # genre blocked the built one ("another master has the same place") and
    # itself ("a file with no record is in the way", which was false).
    _master(cfg, "Soft Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h2")
    _build(cfg, eb.IPHONE_KIND)  # h2 is on the phone
    _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")  # sorts first
    plan, _, phone, _ = _build(cfg, eb.IPHONE_KIND)
    assert plan.up_to_date == 1 and set(phone) == {"h2"}
    assert [m.audio_hash for m, _ in plan.blocked] == ["h1"]
    assert "same place" in plan.blocked[0][1]


def test_copies_kept_after_a_catalogue_wipe_count_against_the_budget(cfg):
    # Cloud review of #53: after the between-batches wipe, the copies of
    # masters no longer catalogued are kept (right) but were not charged to
    # the budget, so each budgeted build put a whole budget on top of them.
    from musaeus.editions import IPHONE, estimated_bytes, load_tracks

    def six_seconds():
        c = sqlite3.connect(cfg.db_path)
        c.execute("UPDATE archive SET duration = 6.0")
        c.commit()
        c.close()

    old = [_master(cfg, f"Rock/Old/Al/Old - T{i}.m4a", f"o{i}") for i in range(3)]
    six_seconds()
    _build(cfg, eb.IPHONE_KIND, allowed={str(p) for p in old})
    kept = sum(p.stat().st_size for p in cfg.iphone_library.rglob("*.m4a"))
    wipe = sqlite3.connect(cfg.db_path)
    wipe.execute("DELETE FROM archive")  # "AUDIT PASSED: safe to snapshot and wipe"
    wipe.commit()
    wipe.close()
    for i in range(3):
        _master(cfg, f"Rock/New/Al/New - T{i}.m4a", f"n{i}")
    six_seconds()
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    one = max(estimated_bytes(t, IPHONE) for t in load_tracks(conn))
    budget = kept + int(1.5 * one)  # room for the kept copies and one new track
    ledger = open_ledger(ledger_path(cfg))
    try:
        plan, _ = eb.budgeted(
            conn, ledger, cfg.alac_archive, cfg.iphone_library, eb.IPHONE_KIND, budget
        )
    finally:
        conn.close()
        ledger.close()
    assert plan.kept_unselected == 3
    assert len(plan.bake) == 1, f"{len(plan.bake)} new tracks for room for one"
    assert kept + plan.bake_bytes <= budget


def _preview(cfg, *args: str) -> subprocess.CompletedProcess[str]:
    """`musaeus edition ...` (the preview) against this test's vault."""
    import os
    import sys

    env = {**os.environ, "MUSAEUS_VAULT_ROOT": str(cfg.vault_root),
           "MUSAEUS_DB_PATH": str(cfg.db_path)}  # fmt: skip
    return subprocess.run(
        [sys.executable, "-m", "musaeus.cli", "edition", *args],
        capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
        cwd=Path(__file__).resolve().parents[1], timeout=120,
    )  # fmt: skip


def test_the_preview_lists_what_the_build_would_make(cfg):
    # Cloud review of #53: `musaeus edition iphone --budget-gb N` picked with
    # plain select_edition -- it listed a master the build blocks (does not
    # decode) and left out the one the build makes.
    from musaeus.editions import IPHONE, estimated_bytes, load_tracks

    _master(cfg, "Rock/Aa/Al/Aa - First.m4a", "h1")
    _master(cfg, "Rock/Zz/Al/Zz - Last.m4a", "h2")
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    deep_scan.ensure_columns(conn)
    conn.execute("UPDATE archive SET decode_ok = 0, title = 'First' WHERE audio_hash = 'h1'")
    conn.execute("UPDATE archive SET title = 'Last' WHERE audio_hash = 'h2'")
    conn.commit()
    one = max(estimated_bytes(t, IPHONE) for t in load_tracks(conn))
    conn.close()
    r = _preview(cfg, "iphone", "--budget-gb", f"{1.5 * one / 1e9:.9f}", "--list")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Last" in r.stdout and "First" not in r.stdout, r.stdout


@pytest.mark.parametrize("gb", ["0", "nan", "inf", "-1"])
def test_a_budget_that_is_not_a_size_is_refused(cfg, gb):
    # --budget-gb 0 previewed the whole library; nan and inf crashed.
    _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    for r in (_preview(cfg, "iphone", "--budget-gb", gb),
              _cli(cfg, "iphone", "--budget-gb", gb, "--dry-run")):  # fmt: skip
        assert r.returncode != 0 and "Traceback" not in r.stderr, r.stdout + r.stderr
        assert "more than 0" in r.stderr, r.stderr


def test_a_wrong_kept_measurement_is_measured_again(cfg, monkeypatch):
    # Cloud review of #53: a kept measurement was reused for ever -- one 6 LU
    # off failed three builds in a row and was never re-measured. A bake that
    # fails on a kept measurement is tried once more on a fresh one.
    from musaeus.edition_ledger import keep_measurement, measurements_of

    _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    _build(cfg, eb.CAR_KIND)  # measures and keeps it
    ledger = open_ledger(ledger_path(cfg))
    ((recipe, good),) = measurements_of(ledger, "h1").items()
    keep_measurement(ledger, "h1", recipe, {**good, "input_i": str(float(good["input_i"]) - 6)})
    ledger.close()
    _, out, phone, _ = _build(cfg, eb.IPHONE_KIND)
    assert out.baked == 1 and not out.failed, out.failed
    ledger = open_ledger(ledger_path(cfg))
    kept = measurements_of(ledger, "h1")[recipe]
    ledger.close()
    assert abs(float(kept["input_i"]) - float(good["input_i"])) < 0.2, "the bad one was kept"


@pytest.mark.parametrize(
    "setting,value",
    [("NOISE_LEVELS_DB", {"brown": -13.0, "pink": -15.0, "white": -18.0}),
     ("AAC_BITRATE", "192k"), ("CEILING", 0.95)],
)  # fmt: skip
def test_a_changed_setting_rebuilds_the_car_copies_once(cfg, monkeypatch, setting, value):
    # Grey, 2026-09-29, on the cloud review of #53 (finding 8): after a car
    # setting changes, the copies made the old way are made again -- one
    # edition never holds copies made two ways.
    _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    _build(cfg, eb.CAR_KIND)
    monkeypatch.setattr(edition_bake, setting, value)
    plan, out, car, _ = _build(cfg, eb.CAR_KIND)
    assert len(plan.rebake) == 1 and out.baked == 1 and not out.failed, out.failed
    plan, out, _, _ = _build(cfg, eb.CAR_KIND)
    assert plan.up_to_date == 1 and not plan.bake, "rebuilt again with nothing changed"


def test_a_lossless_copy_is_never_rebuilt_for_a_car_setting(cfg, monkeypatch):
    _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    _build(cfg, eb.LOSSLESS_KIND)
    monkeypatch.setattr(edition_bake, "AAC_BITRATE", "192k")
    plan, _, _, _ = _build(cfg, eb.LOSSLESS_KIND)
    assert plan.up_to_date == 1 and not plan.rebake


def test_a_retag_does_not_hide_a_changed_setting(cfg, monkeypatch):
    from mutagen.mp4 import MP4

    master = _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    _build(cfg, eb.CAR_KIND)
    f = MP4(master)
    f.tags["\xa9nam"] = ["Angie (Remaster)"]
    f.save()
    monkeypatch.setattr(edition_bake, "AAC_BITRATE", "192k")
    plan, _, _, _ = _build(cfg, eb.CAR_KIND)
    assert len(plan.rebake) == 1, "a new master mtime must not stand for new settings"


def test_a_car_build_writes_the_playlists_that_travel_with_it(cfg):
    # Cloud review of #53, finding 9: the retired builder's last step wrote
    # M3U8s into CAR_Library/Playlists, so they go to the USB with the songs
    # (Grey, 2026-09-17). The new build never wrote them, and a stale one was
    # copied to the stick as it was.
    _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    conn = sqlite3.connect(cfg.db_path)
    conn.execute(
        "UPDATE archive SET genre = 'Rock', artist = 'The Rolling Stones', title = 'Angie'"
    )
    conn.commit()
    conn.close()
    playlists = cfg.car_library / "Playlists"
    playlists.mkdir(parents=True)
    (playlists / "Holiday.m3u8").write_text("#EXTM3U\n../Old/Gone/Old - Gone.m4a\n")
    r = _cli(cfg, "car")
    assert r.returncode == 0, r.stdout + r.stderr
    rock = playlists / "Rock.m3u8"
    assert rock.is_file(), r.stdout
    assert "../Stones/Hits/The Rolling Stones - Angie.m4a" in rock.read_text()
    assert not (playlists / "Holiday.m3u8").exists(), "a stale playlist travelled on"


def test_the_index_script_writes_where_the_car_edition_is(cfg, tmp_path):
    # ...and write_car_index.py built its folder from the vault's Libraries,
    # ignoring MUSAEUS_CAR_LIBRARY.
    import os
    import sys

    _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    moved = tmp_path / "Elsewhere" / "MyCar"
    moved.mkdir(parents=True)
    env = {**os.environ, "MUSAEUS_VAULT_ROOT": str(cfg.vault_root),
           "MUSAEUS_DB_PATH": str(cfg.db_path), "MUSAEUS_CAR_LIBRARY": str(moved)}  # fmt: skip
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run(
        [sys.executable, str(root / "scripts" / "car_library" / "write_car_index.py")],
        capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=120,
    )  # fmt: skip
    assert r.returncode == 0 and str(moved) in r.stdout, r.stdout + r.stderr


def test_a_partial_edition_has_no_empty_playlists(cfg):
    # The 200-song vault build, 2026-09-29: the playlist stage writes a list
    # per genre of the whole catalogue, and a genre with no copy in this
    # edition yet (a partial car build; every budgeted iPhone edition) came
    # out as an empty playlist -- and failed the build's check.
    _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    _master(cfg, "Jazz/Miles/Kind/Miles Davis - So What.m4a", "h2")
    conn = sqlite3.connect(cfg.db_path)
    conn.execute(
        "UPDATE archive SET genre = 'Rock', artist = 'Stones', title = 'Angie' WHERE audio_hash = 'h1'"
    )
    conn.execute(
        "UPDATE archive SET genre = 'Jazz', artist = 'Miles', title = 'So What' WHERE audio_hash = 'h2'"
    )
    conn.commit()
    conn.close()
    r = _cli(cfg, "car", "--limit", "1")  # Jazz sorts first: only it is built
    assert r.returncode == 0, r.stdout + r.stderr
    names = sorted(p.name for p in (cfg.car_library / "Playlists").glob("*.m3u8"))
    assert "Rock.m3u8" not in names and "Jazz.m3u8" in names, names


def test_a_budget_never_deletes_the_copy_of_a_master_that_cannot_move(cfg):
    # Second review of #53, 1: a master whose copy cannot move (its new place
    # is taken) keeps its copy without a budget; with one -- even one that
    # everything fits in -- it was counted over budget and its copy deleted.
    a = _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    _build(cfg, eb.IPHONE_KIND)
    old = cfg.iphone_library / "Stones" / "Hits" / "The Rolling Stones - Angie.m4a"
    moved = cfg.alac_archive / "Rock" / "Stones" / "Best Of" / "The Rolling Stones - Angie.m4a"
    moved.parent.mkdir(parents=True)
    a.rename(moved)
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("UPDATE archive SET file_path = ? WHERE audio_hash = 'h1'", (str(moved),))
    conn.commit()
    conn.close()
    stranger = cfg.iphone_library / "Stones" / "Best Of" / "The Rolling Stones - Angie.m4a"
    stranger.parent.mkdir(parents=True)
    stranger.write_bytes(b"\\0")  # the new place is taken
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    ledger = open_ledger(ledger_path(cfg))
    try:
        plan, _ = eb.budgeted(
            conn, ledger, cfg.alac_archive, cfg.iphone_library, eb.IPHONE_KIND, 10**12
        )
    finally:
        conn.close()
        ledger.close()
    assert not plan.remove and not plan.over_budget, (plan.remove, plan.over_budget)
    assert old.exists()
    assert any("new place is taken" in why for _, why in plan.blocked)


def test_a_failed_bake_is_retried_only_when_it_used_a_kept_measurement(cfg, monkeypatch):
    # Second review of #53, 2: the retry fired whenever the record held ANY
    # measurement of the song (here the Lossless one), so every real failure
    # was baked twice -- and a stopped build re-made the songs it had killed.
    _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    _build(cfg, eb.LOSSLESS_KIND)  # keeps the Lossless recipe's measurement
    calls = []

    def broken(src, tmp, *, noise, known=None):
        calls.append(known)
        raise edition_bake.BakeError("a click")

    monkeypatch.setattr(edition_bake, "bake_aac", broken)
    _, out, _, _ = _build(cfg, eb.CAR_KIND)
    assert out.failed and len(calls) == 1, calls


def test_a_stopping_build_does_not_retry(monkeypatch, tmp_path):
    calls = []

    def reused_fail(src, tmp, known):
        calls.append(1)
        err = edition_bake.BakeError("killed")
        err.reused = True
        raise err

    kind = eb.Kind("car", "-14.0", "car_library", eb._artist_album, reused_fail, include_lossy=True)
    m = eb.Master(tmp_path / "m.m4a", "h", "alac", -20.0, -3.0, 1, 1, decode_ok=1)
    edition_bake.STOPPING.set()
    try:
        with pytest.raises(edition_bake.BakeError):
            eb._bake_one(m, tmp_path / "c.m4a", kind, {"r": {}})
    finally:
        edition_bake.STOPPING.clear()
    assert calls == [1]


def test_no_ffmpeg_is_left_running_when_fdkaac_cannot_start(tmp_path, monkeypatch):
    # Second review of #53, 4: ffmpeg started first and was recorded only
    # once fdkaac had started too; with fdkaac missing it ran on, untracked,
    # into a pipe nobody read.
    import time

    started = []
    real = subprocess.Popen

    def spy(cmd, *a, **k):
        p = real(cmd, *a, **k)
        started.append((cmd[0], p))
        return p

    master = tmp_path / "m.m4a"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=4", "-ac", "2", "-c:a", "alac", str(master)],
        check=True,
    )  # fmt: skip
    monkeypatch.setattr(edition_bake, "FDKAAC", str(tmp_path / "no-such-fdkaac"))
    monkeypatch.setattr(subprocess, "Popen", spy)
    with pytest.raises(edition_bake.BakeError, match="could not start"):
        edition_bake.bake_aac(master, tmp_path / "c.m4a", noise=False)
    monkeypatch.undo()
    time.sleep(0.5)
    left = [name for name, p in started if name == "ffmpeg" and p.poll() is None]
    assert not left, "an ffmpeg was left running"


def _events(cfg) -> int:
    conn = sqlite3.connect(cfg.db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()


def test_a_car_build_writes_nothing_to_the_catalogue(cfg):
    # Second review of #53, 5: edition-build is read-only on the catalogue,
    # but its playlist step logged a run's start and end into its events.
    _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("UPDATE archive SET genre = 'Rock', artist = 'Stones', title = 'Angie'")
    conn.commit()
    conn.close()
    before = _events(cfg)
    r = _cli(cfg, "car")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (cfg.car_library / "Playlists" / "Rock.m3u8").is_file()
    assert _events(cfg) == before, "the build wrote to the catalogue"


def test_playlists_are_left_as_they_are_when_nothing_is_catalogued(cfg):
    # Second review of #53, 3: with nothing catalogued (a wipe), the playlist
    # step wrote nothing, the sweep then deleted every playlist, and the
    # build said nothing.
    _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    _build(cfg, eb.CAR_KIND)
    playlists = cfg.car_library / "Playlists"
    playlists.mkdir(parents=True, exist_ok=True)
    kept = playlists / "Rock.m3u8"
    kept.write_text("#EXTM3U\n../Stones/Hits/The Rolling Stones - Angie.m4a\n")
    wipe = sqlite3.connect(cfg.db_path)
    wipe.execute("DELETE FROM archive")
    wipe.commit()
    wipe.close()
    r = _cli(cfg, "car")
    assert r.returncode == 0, r.stdout + r.stderr
    assert kept.is_file(), "the playlists were swept away"
    assert "left as they were" in r.stdout, r.stdout


def test_a_car_build_refuses_to_start_without_fdkaac(cfg, monkeypatch, capsys):
    # Second review of #53, 6: nothing checked for fdkaac; a build measured
    # every song, then failed them all.
    from musaeus import cli

    _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    monkeypatch.setattr(cli, "get_config", lambda: cfg)
    monkeypatch.setattr(edition_bake, "FDKAAC", "/nonexistent/fdkaac")
    monkeypatch.setenv("MUSAEUS_NO_SLEEP_INHIBIT", "1")
    args = cli._build_parser().parse_args(["edition-build", "car"])
    assert cli._cmd_edition_build(args) != 0
    assert "fdkaac" in capsys.readouterr().err
    assert not list(cfg.car_library.rglob("*.m4a")) if cfg.car_library.exists() else True


def test_the_doctor_names_a_missing_fdkaac():
    from musaeus import doctor

    assert "fdkaac" in {name for name, *_ in doctor._EXTERNAL_TOOLS}


def test_an_adopted_car_copy_is_made_again_with_todays_settings(cfg):
    # Second review of #53, 9: a copy left by a stopped build was recorded as
    # made with today's settings, though its marker cannot say what made it
    # -- a copy from before a settings change was never made again.
    master = _master(cfg, "Rock/Stones/Hits/The Rolling Stones - Angie.m4a", "h1")
    target = eb.CAR_KIND.place(cfg.alac_archive, cfg.car_library, master)
    target.parent.mkdir(parents=True, exist_ok=True)
    edition_bake.bake_aac(master, target, noise=True)
    edition_bake.copy_tags(master, target, eb.marker_for("h1", eb.CAR_KIND))  # stopped here
    _, out, _, _ = _build(cfg, eb.CAR_KIND)
    assert out.adopted == 1
    plan, _, _, _ = _build(cfg, eb.CAR_KIND)
    assert plan.resettled == 1, "an adopted copy of unknown make was taken for today's"


def test_an_odd_temp_folder_name_does_not_break_the_encode(tmp_path, monkeypatch):
    # Second review of #53, 10: the peaks file's path went into the filter
    # graph unescaped; a temp folder with ':' or ',' broke every copy.
    import tempfile

    odd = tmp_path / "tmp:odd,dir"
    odd.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(odd))
    master = tmp_path / "m.m4a"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=4", "-f", "lavfi", "-i", "sine=frequency=660:duration=4",
         "-filter_complex", "[0]volume=-4dB[a];[1]volume=-9dB[b];[a][b]concat=n=2:v=0:a=1",
         "-ac", "2", "-c:a", "alac", str(master)],
        check=True,
    )  # fmt: skip
    edition_bake.bake_aac(master, tmp_path / "c.m4a", noise=True)
