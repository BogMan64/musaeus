"""Review of #86, finding 13 (2026-10-07), in the legacy `musaeus transcode`
export: ffmpeg wrote straight to the final file with no -nostdin, so a keypress
("q") left a truncated file under the final name, recorded as done; and the
sample rate and channel count were not set, so they came from the source (a
96 kHz or 5.1 export). Now: -nostdin, written to .part and renamed only after
ffmpeg succeeds, stereo at 44.1 or 48 kHz.
"""

from __future__ import annotations

import subprocess

import pytest

from musaeus.stages import transcode


@pytest.fixture
def captured(monkeypatch):
    calls = {}

    def run(cmd, **kwargs):
        calls["cmd"], calls["kwargs"] = cmd, kwargs
        return subprocess.CompletedProcess(cmd, 1, "", "killed")  # ffmpeg fails

    monkeypatch.setattr(transcode.subprocess, "run", run)
    monkeypatch.setattr(transcode, "_probe_streams",
                        lambda p: {"streams": [{"codec_type": "audio", "sample_rate": "96000", "channels": 6}]})  # fmt: skip
    return calls


def test_the_export_never_reads_the_terminal_and_sets_its_format(tmp_path, captured):
    dst = tmp_path / "out" / "song.m4a"
    with pytest.raises(ValueError):
        transcode._transcode_file(tmp_path / "src.flac", dst, "aac", "A", "B", "T", "", "", "")

    cmd = captured["cmd"]
    assert "-nostdin" in cmd and captured["kwargs"].get("stdin") == subprocess.DEVNULL
    assert cmd[cmd.index("-ac") + 1] == "2"
    assert cmd[cmd.index("-ar") + 1] == "44100", "a 96 kHz source made a 96 kHz export"
    assert cmd[-1].endswith(".part"), "ffmpeg wrote straight to the final name"


def test_a_failed_export_leaves_nothing_under_the_final_name(tmp_path, captured):
    dst = tmp_path / "out" / "song.m4a"
    with pytest.raises(ValueError):
        transcode._transcode_file(tmp_path / "src.flac", dst, "aac", "A", "B", "T", "", "", "")

    assert not dst.exists() and not list(dst.parent.glob("*.part"))
