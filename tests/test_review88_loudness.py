"""Review of #88, finding 2 (2026-10-07): the loudness measurement that ForgeStage
writes into every master's ReplayGain and R128 tags.

ffmpeg ran without -nostdin, so in the interactive console a keypress ("q") ended
the measurement early and a two-second reading (-61.75 LUFS against -27.85) went
into the master's tags as "ok". A run that exited non-zero was trusted whenever it
had printed its JSON, and every audio stream was measured where the bake measures
the first. The bake already refuses a failed run; the two now agree.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from musaeus import loudness

_JSON = (
    '{"input_i" : "-27.85", "input_tp" : "-1.20", "input_lra" : "5.0", "input_thresh" : "-38.0"}'
)


def test_ffmpeg_never_reads_the_terminal_and_measures_the_first_stream(monkeypatch, tmp_path):
    seen = {}

    class Stop(Exception):
        pass

    def popen(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        raise Stop

    monkeypatch.setattr(loudness.subprocess, "Popen", popen)
    with pytest.raises(Stop):
        loudness._run_loudnorm(tmp_path / "a.m4a", linear=False, duration=10.0)
    assert "-nostdin" in seen["cmd"]
    assert seen["kwargs"].get("stdin") == subprocess.DEVNULL
    assert seen["cmd"][seen["cmd"].index("-map") + 1] == "0:a:0"


def test_a_failed_run_is_never_ok(monkeypatch, tmp_path):
    song = tmp_path / "a.m4a"
    song.write_bytes(b"x")
    monkeypatch.setattr(loudness, "_get_duration", lambda p: 10.0)
    monkeypatch.setattr(loudness, "_run_loudnorm", lambda p, linear, duration: (1, _JSON))

    lufs, tp, reason = loudness.measure_loudness(song)

    assert reason != "ok" and lufs is None


def test_a_clean_run_is_ok(monkeypatch, tmp_path):
    song = tmp_path / "a.m4a"
    song.write_bytes(b"x")
    monkeypatch.setattr(loudness, "_get_duration", lambda p: 10.0)
    monkeypatch.setattr(loudness, "_run_loudnorm", lambda p, linear, duration: (0, _JSON))

    assert loudness.measure_loudness(song) == (-27.85, -1.2, "ok")


# ── Codex audit 2026-10-16, F10 ───────────────────────────────────────────────
# A truncated file decodes to a clean exit 0 with "partial file" errors, and
# loudnorm measured the part that was there. Forge stored the reading, stamped
# rg_tagged_at and wrote the gains into the master.

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg"
)


def _song(path, keep=1.0):
    """A real 12 s ALAC, moov first (so its tags stay readable), cut to *keep*."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "anoisesrc=seed=3:color=pink:duration=12", "-c:a", "alac",
         "-movflags", "+faststart", str(path)],
        check=True, stdin=subprocess.DEVNULL,
    )  # fmt: skip
    if keep < 1.0:
        data = path.read_bytes()
        path.write_bytes(data[: int(len(data) * keep)])
    return path


@needs_ffmpeg
def test_a_truncated_file_is_damaged_not_measured(tmp_path):
    lufs, tp, reason = loudness.measure_loudness(_song(tmp_path / "cut.m4a", keep=0.6))
    assert (lufs, tp, reason) == (None, None, "damaged")


@needs_ffmpeg
def test_a_whole_file_is_still_measured(tmp_path):
    lufs, _tp, reason = loudness.measure_loudness(_song(tmp_path / "whole.m4a"))
    assert reason == "ok" and lufs is not None


@needs_ffmpeg
def test_forge_stores_and_writes_nothing_for_a_truncated_master(tmp_path):
    from mutagen.mp4 import MP4

    from musaeus.config import MusicConfig
    from musaeus.context import RunContext
    from musaeus.db import open_db, upsert_archive
    from musaeus.stages.forge import ForgeStage

    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )
    ctx = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    master = _song(tmp_path / "cut.m4a", keep=0.6)
    upsert_archive(ctx.conn, {"file_path": str(master), "status": "CATALOGUED", "ext": ".m4a",
                              "artist": "A", "album": "B", "title": "C"})  # fmt: skip
    ctx.conn.commit()

    result = ForgeStage().run(ctx)

    row = ctx.conn.execute(
        "SELECT lufs, rg_gain, rg_tagged_at FROM archive WHERE file_path = ?", (str(master),)
    ).fetchone()
    assert tuple(row) == (None, None, None), "a reading of half a song was stored"
    tags = MP4(master).tags or {}
    assert not [k for k in tags if "R128" in k or "replaygain" in k.lower()], "gains written"
    assert result.success is False
    assert any("damaged" in n for n in result.notes)
    assert any("cut.m4a" in e and "damaged" in e for e in result.errors), "the file is not named"


def test_an_artwork_error_is_not_damage_to_the_music(monkeypatch, tmp_path):
    from musaeus.stages import corrupt

    monkeypatch.setattr(corrupt, "audio_stream_index", lambda p: 0)
    stderr = (
        "[info] Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'a.m4a':\n"
        "[mjpeg @ 0x55d0c0a1b2c0] [error] unable to decode APP fields: Invalid data\n"
        "[Parsed_loudnorm_0 @ 0x55d0c0a1b2c0] [info] \n" + _JSON + "\n"
    )
    assert loudness._decode_damage(stderr, tmp_path / "a.m4a") == ""
    damaged = (
        stderr
        + "[mov,mp4,m4a,3gp,3g2,mj2 @ 0x55d0c0a1b2c0] [error] stream 0, offset 0xeaa50: partial file\n"
    )
    assert "partial file" in loudness._decode_damage(damaged, tmp_path / "a.m4a")


def test_ffmpeg_7s_two_prefixes_before_an_error_are_read(monkeypatch, tmp_path):
    # Review of #198, finding 1: ffmpeg 7.0 prints a decoder's error after two
    # "[ctx @ 0x..]" prefixes; with one allowed, mid-stream damage read as clean.
    from musaeus.stages import corrupt

    monkeypatch.setattr(corrupt, "audio_stream_index", lambda p: 0)
    line = (
        "[aist#0:0/alac @ 0x5600a1b2c3d0] [dec:alac @ 0x5600a1b2c4e0] [error] "
        "Error submitting packet to decoder: Invalid data found when processing input\n"
    )
    assert "Error submitting packet" in loudness._decode_damage(line, tmp_path / "a.m4a")
