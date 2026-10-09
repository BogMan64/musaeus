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


# ── Review of #124 (2026-10-08): .part then rename, but nothing verified in
# between. ffmpeg exits 0 on a truncated source ("partial file" on stderr),
# and the short export was renamed into place and skipped for ever after.


def _ffmpeg_exits_0(monkeypatch, stderr: str) -> None:
    def run(cmd, **kwargs):
        from pathlib import Path

        Path(cmd[-1]).write_bytes(b"an export")  # the .part ffmpeg wrote
        return subprocess.CompletedProcess(cmd, 0, "", stderr)

    monkeypatch.setattr(transcode.subprocess, "run", run)
    monkeypatch.setattr(transcode, "_probe_streams", lambda p: {"streams": []})


def test_a_source_ffmpeg_calls_partial_is_not_exported(tmp_path, monkeypatch):
    _ffmpeg_exits_0(monkeypatch, "[aac @ 0x55] Input buffer exhausted before END element found\n")
    monkeypatch.setattr(transcode, "decodes_cleanly", lambda p: (True, None), raising=False)
    dst = tmp_path / "out" / "song.m4a"
    with pytest.raises(ValueError, match="Input buffer exhausted"):
        transcode._transcode_file(tmp_path / "src.flac", dst, "aac", "A", "B", "T", "", "", "")
    assert not dst.exists() and not list(dst.parent.iterdir())


def test_an_export_that_does_not_decode_is_not_renamed(tmp_path, monkeypatch):
    _ffmpeg_exits_0(monkeypatch, "")
    monkeypatch.setattr(transcode, "decodes_cleanly", lambda p: (False, "Invalid data found"),
                        raising=False)  # fmt: skip
    dst = tmp_path / "out" / "song.m4a"
    with pytest.raises(ValueError, match="Invalid data found"):
        transcode._transcode_file(tmp_path / "src.flac", dst, "aac", "A", "B", "T", "", "", "")
    assert not dst.exists() and not list(dst.parent.iterdir())


def test_a_clean_export_is_renamed_into_place(tmp_path, monkeypatch):
    _ffmpeg_exits_0(monkeypatch, "")
    monkeypatch.setattr(transcode, "decodes_cleanly", lambda p: (True, None), raising=False)
    dst = tmp_path / "out" / "song.m4a"
    transcode._transcode_file(tmp_path / "src.flac", dst, "aac", "A", "B", "T", "", "", "")
    assert dst.read_bytes() == b"an export"


# ── Review of #129-#134, finding 9: a source cut on a frame boundary exports
# short with no error at all, and the audio stream is not always input 0. ──


def test_an_export_shorter_than_its_source_is_refused(tmp_path, monkeypatch):
    _ffmpeg_exits_0(monkeypatch, "")
    monkeypatch.setattr(transcode, "decodes_cleanly", lambda p: (True, None))
    monkeypatch.setattr(
        transcode,
        "stream_seconds",
        lambda p: 9.6 if p.name.endswith(".part") else 20.0,
        raising=False,
    )
    dst = tmp_path / "out" / "song.m4a"
    with pytest.raises(ValueError, match="9.6"):
        transcode._transcode_file(tmp_path / "src.flac", dst, "aac", "A", "B", "T", "", "", "")
    assert not dst.exists() and not list(dst.parent.iterdir())


def test_an_audio_error_on_the_real_audio_stream_is_read(tmp_path, monkeypatch):
    """Cover art first: the audio is input stream 1, and its decode errors
    were dropped as if they were about the picture."""
    _ffmpeg_exits_0(monkeypatch, "Error while decoding stream #0:1: Invalid data found\n")
    monkeypatch.setattr(transcode, "_probe_streams", lambda p: {"streams": [
        {"index": 0, "codec_type": "video"}, {"index": 1, "codec_type": "audio"}]})  # fmt: skip
    monkeypatch.setattr(transcode, "decodes_cleanly", lambda p: (True, None))
    dst = tmp_path / "out" / "song.m4a"
    with pytest.raises(ValueError, match="Invalid data"):
        transcode._transcode_file(tmp_path / "src.flac", dst, "aac", "A", "B", "T", "", "", "")


def test_an_export_shorter_than_the_catalogue_recorded_is_refused(tmp_path, monkeypatch):
    """A source cut on a sample boundary reads as a whole, shorter file; only the
    length recorded at intake shows it is short."""
    _ffmpeg_exits_0(monkeypatch, "")
    monkeypatch.setattr(transcode, "decodes_cleanly", lambda p: (True, None))
    monkeypatch.setattr(transcode, "stream_seconds", lambda p: 10.0, raising=False)
    dst = tmp_path / "out" / "song.m4a"
    with pytest.raises(ValueError, match="10.0"):
        transcode._transcode_file(tmp_path / "src.wav", dst, "aac", "A", "B", "T", "", "", "",
                                  recorded_seconds=20.0)  # fmt: skip
    assert not dst.exists()
