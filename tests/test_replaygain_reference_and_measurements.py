"""ReplayGain at -18 LUFS, and the loudness measurements kept in the masters (2026-10-06).

The masters' replaygain_track_gain was written from the -23 LUFS (R128) gain, so every player
that reads ReplayGain played them 5 dB too quietly. And the edition ledger was the only copy of
the masters' loudness measurements; they now live in the masters too, and not in the copies.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from mutagen.mp4 import MP4, MP4FreeForm

from musaeus import edition_bake, master_measurements
from musaeus.edition_ledger import keep_measurement, measurements_of, open_ledger
from musaeus.stages.forge import _write_tags_m4a, write_rg_tags

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.write_master_loudness_tags import RG_KEY, wanted  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg")
M = {"input_i": "-13.62", "input_lra": "2.80", "input_tp": "-0.99", "input_thresh": "-23.89"}


def _m4a(path: Path) -> Path:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1", "-c:a", "alac", str(path)],
                   check=True, capture_output=True)  # fmt: skip
    return path


def _tag(path, key):
    raw = (MP4(path).tags or {}).get(key)
    return bytes(raw[0]).decode() if raw else None


@needs_ffmpeg
def test_replaygain_is_5_db_above_the_r128_gain(tmp_path):
    # a master at -17.46 LUFS: R128 -5.54 dB (at -23), ReplayGain -0.54 dB (at -18)
    p = _m4a(tmp_path / "m.m4a")
    _write_tags_m4a(p, -23 - (-17.46), 0.9)
    assert _tag(p, "----:com.apple.iTunes:R128_TRACK_GAIN") == str(round(-5.54 * 256))
    assert _tag(p, RG_KEY) == "-0.54 dB"


@needs_ffmpeg
def test_a_gain_given_at_minus_18_is_moved_to_minus_23_for_r128(tmp_path):
    p = _m4a(tmp_path / "m.m4a")
    write_rg_tags(p, -0.54, 0.9)  # reference -18, no r128 gain given
    assert _tag(p, "----:com.apple.iTunes:R128_TRACK_GAIN") == str(round(-5.54 * 256))
    assert _tag(p, RG_KEY) == "-0.54 dB"


@needs_ffmpeg
def test_measurements_round_trip_through_the_master(tmp_path):
    p = _m4a(tmp_path / "m.m4a")
    a = MP4(p)
    if a.tags is None:
        a.add_tags()
    master_measurements.put(a.tags, {"alac I=-18.0 TP=-1.0 LRA=50.0": M})
    a.save()
    assert master_measurements.read(p) == {"alac I=-18.0 TP=-1.0 LRA=50.0": M}


@needs_ffmpeg
def test_a_lost_ledger_is_refilled_from_the_master_and_nothing_is_overwritten(tmp_path):
    p = _m4a(tmp_path / "m.m4a")
    a = MP4(p)
    if a.tags is None:
        a.add_tags()
    master_measurements.put(a.tags, {"alac": M, "aac": {**M, "input_i": "-14.00"}})
    a.save()
    conn = open_ledger(tmp_path / "editions.db")
    keep_measurement(conn, "h1", "aac", {"input_i": "-99"})  # already kept: stays as it is
    assert master_measurements.restore_ledger(conn, "h1", p) == 1
    got = measurements_of(conn, "h1")
    assert got["alac"] == M and got["aac"] == {"input_i": "-99"}


@needs_ffmpeg
def test_copies_get_neither_the_gain_nor_the_measurements(tmp_path):
    master, copy = _m4a(tmp_path / "m.m4a"), _m4a(tmp_path / "c.m4a")
    _write_tags_m4a(master, -5.54, 0.9)
    a = MP4(master)
    master_measurements.put(a.tags, {"alac": M})
    a.tags["\xa9nam"] = ["Song"]
    a.save()
    edition_bake.copy_tags(master, copy, "marker")
    tags = MP4(copy).tags
    assert master_measurements.KEY not in tags and RG_KEY not in tags
    assert tags["\xa9nam"] == ["Song"]


def test_the_driver_wants_only_what_is_wrong():
    right = {"----:com.apple.iTunes:R128_TRACK_GAIN": [MP4FreeForm(b"-1418")],
             RG_KEY: [MP4FreeForm(b"-0.54 dB")],
             master_measurements.KEY: [MP4FreeForm(master_measurements.encode({"a": M}))]}  # fmt: skip
    assert wanted(right, -17.46, {"a": M}) == {}
    old = {**right, RG_KEY: [MP4FreeForm(b"-5.54 dB")]}  # the -23 value, as written before
    assert wanted(old, -17.46, {"a": M}) == {RG_KEY: b"-0.54 dB"}
    assert set(wanted({}, -17.46, {"a": M})) == {
        "----:com.apple.iTunes:R128_TRACK_GAIN", RG_KEY, master_measurements.KEY
    }  # fmt: skip


def test_no_loudness_known_means_no_gain_written():
    assert wanted({}, None, {}) == {}


def test_the_ledger_tables_exist_in_a_new_ledger(tmp_path):
    conn = open_ledger(tmp_path / "e.db")
    assert isinstance(conn, sqlite3.Connection)
