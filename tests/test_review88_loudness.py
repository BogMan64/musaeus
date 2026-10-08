"""Review of #88, finding 2 (2026-10-07): the loudness measurement that ForgeStage
writes into every master's ReplayGain and R128 tags.

ffmpeg ran without -nostdin, so in the interactive console a keypress ("q") ended
the measurement early and a two-second reading (-61.75 LUFS against -27.85) went
into the master's tags as "ok". A run that exited non-zero was trusted whenever it
had printed its JSON, and every audio stream was measured where the bake measures
the first. The bake already refuses a failed run; the two now agree.
"""

from __future__ import annotations

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
