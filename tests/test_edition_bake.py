"""The loudness bake behind the Lossless edition (musaeus/edition_bake.py).

Real ffmpeg on a synthetic tone: the bake's whole job is what ffmpeg does to
real audio, so a mock would test nothing. Skipped when ffmpeg is absent.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus import edition_bake as eb

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not available",
)


def _master(path: Path, *, rate: int = 44_100, fmt: str = "s16p", gain_db: float = -6.0) -> Path:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration=8:sample_rate={rate}",
            "-af", f"volume={gain_db}dB",
            "-c:a", "alac", "-sample_fmt", fmt, str(path),
        ],
        check=True,
    )  # fmt: skip
    return path


def _tag(path: Path, **freeform: str) -> None:
    from mutagen.mp4 import MP4, MP4FreeForm

    f = MP4(path)
    if f.tags is None:
        f.add_tags()
    f.tags["\xa9nam"] = ["Sunshine Superman"]
    f.tags["tmpo"] = [135]
    for k, v in freeform.items():
        f.tags[f"----:com.apple.iTunes:{k}"] = [MP4FreeForm(v.encode())]
    f.save()


def test_a_bake_lands_at_minus_18_and_keeps_the_masters_rate_and_depth(tmp_path):
    master = _master(tmp_path / "m.m4a", rate=44_100, fmt="s16p")
    out = tmp_path / "c.m4a"
    result = eb.bake(master, out)
    assert result.achieved_lufs is not None and abs(result.achieved_lufs + 18.0) <= 0.5
    info = eb.probe(out)
    assert eb.sample_rate_of(info) == 44_100, "loudnorm's 192 kHz must not leak into the copy"
    assert eb.sample_fmt_for(info) == "s16p", "a 16-bit master must not become a 24-bit copy"


def test_the_copy_keeps_the_masters_tags_but_never_its_gain(tmp_path):
    master = _master(tmp_path / "m.m4a")
    _tag(
        master,
        replaygain_track_gain="-9.94 dB",
        replaygain_track_peak="0.88",
        R128_TRACK_GAIN="-2545",
        iTunNORM="00000A",
        initialkey="C# major",
        ISRC="USSM16600249",
    )
    out = tmp_path / "c.m4a"
    eb.bake(master, out)
    eb.copy_tags(master, out, "lossless -18 LUFS master=abc")

    from mutagen.mp4 import MP4

    tags = MP4(out).tags
    keys = {k.lower() for k in tags}
    assert not any("replaygain" in k or "r128" in k or "itunnorm" in k for k in keys), keys
    assert tags["tmpo"] == [135]
    assert bytes(tags["----:com.apple.iTunes:initialkey"][0]) == b"C# major"
    assert bytes(tags["----:com.apple.iTunes:ISRC"][0]) == b"USSM16600249"
    assert eb.read_marker(out) == "lossless -18 LUFS master=abc"


def test_a_file_without_the_marker_is_not_a_copy(tmp_path):
    master = _master(tmp_path / "m.m4a")
    assert eb.read_marker(master) is None
    assert eb.read_marker(tmp_path / "missing.m4a") is None


# ── Cloud review of #49 ──


def test_paused_time_does_not_count_towards_a_deadline(monkeypatch):
    import time

    class AlwaysPaused:
        def __init__(self):
            self.t0 = time.monotonic()

        @property
        def paused_seconds(self):
            return time.monotonic() - self.t0

    monkeypatch.setattr(eb, "_DEADLINE_POLL_S", 0.1)
    monkeypatch.setattr(eb, "ACTIVE_THROTTLE", AlwaysPaused())
    assert eb._run(["sleep", "1.5"], 1).returncode == 0, "a paused child was killed as hung"


def test_a_real_hang_still_times_out(monkeypatch):
    monkeypatch.setattr(eb, "_DEADLINE_POLL_S", 0.1)
    monkeypatch.setattr(eb, "ACTIVE_THROTTLE", None)
    with pytest.raises(eb.BakeError, match="no progress"):
        eb._run(["sleep", "3"], 1)


def test_every_child_runs_with_stdin_closed(tmp_path, monkeypatch):
    # Two workers' ffmpegs shared the terminal: keystrokes read as commands
    # and the tty left without echo (CLAUDE.md: -nostdin in every loop).
    seen = []
    real = subprocess.Popen

    def spy(cmd, *a, **k):
        seen.append((list(cmd), k.get("stdin")))
        return real(cmd, *a, **k)

    master = _master(tmp_path / "m.m4a")
    monkeypatch.setattr(subprocess, "Popen", spy)
    eb.bake(master, tmp_path / "c.m4a")
    eb.decode_problem(master)
    monkeypatch.undo()
    assert seen and all(stdin == subprocess.DEVNULL for _, stdin in seen), seen
    assert all("-nostdin" in cmd for cmd, _ in seen if cmd[0] == "ffmpeg")


def test_a_long_masters_drift_is_judged_by_the_shared_rule(tmp_path, monkeypatch):
    def info(seconds):
        return {
            "streams": [{"codec_type": "audio", "sample_rate": "44100"}],
            "format": {"duration": str(seconds)},
        }

    monkeypatch.setattr(eb, "probe", lambda p: info(603.0))
    eb.verify(info(600.0), tmp_path / "c.m4a", -18.0)  # 3 s of 10 min: within 2 %
    monkeypatch.setattr(eb, "probe", lambda p: info(615.0))
    with pytest.raises(eb.BakeError, match="length changed"):
        eb.verify(info(600.0), tmp_path / "c.m4a", -18.0)


def test_a_compressed_copy_a_little_short_of_the_target_is_kept(tmp_path, monkeypatch):
    # Handel's "Zadok The Priest" reaches -19.5 at best in dynamic mode and
    # was refused on every build. Grey, 2026-09-28: include it at its best
    # level. Only a DYNAMIC copy, only BELOW the target, only up to 2 LU.
    info = {
        "streams": [{"codec_type": "audio", "sample_rate": "44100"}],
        "format": {"duration": "300.0"},
    }
    monkeypatch.setattr(eb, "probe", lambda p: info)
    out = tmp_path / "c.m4a"
    eb.verify(info, out, -19.5, "dynamic")
    for achieved, mode in ((-19.5, "linear"), (-20.5, "dynamic"), (-16.5, "dynamic")):
        with pytest.raises(eb.BakeError, match="wanted"):
            eb.verify(info, out, achieved, mode)


def test_a_wide_range_master_that_needs_no_lift_is_not_compressed(tmp_path):
    # Grey, 2026-09-27: Barenaked Ladies' "Aluminum" (-16.7 LUFS, peak -6.9,
    # range 14.2) only needed turning DOWN 1.3 dB, yet was compressed:
    # loudnorm leaves linear mode when a track's range exceeds the target's
    # (it was 11). Measured on that master: range 20 or 50 -> linear, -18.0.
    master = tmp_path / "wide.m4a"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=12:sample_rate=44100",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=12:sample_rate=44100",
         "-filter_complex",
         "[0]volume=15dB[a];[1]volume=-1dB[b];[a][b]concat=n=2:v=0:a=1",
         "-c:a", "alac", "-sample_fmt", "s16p", str(master)],
        check=True,
    )  # fmt: skip
    measured = eb.ffmpeg_measure_loudnorm(master)
    assert float(measured["input_lra"]) > 11, measured  # the case, not an easy one
    assert float(measured["input_i"]) > -18, measured  # needs turning down, not lifting
    result = eb.bake(master, tmp_path / "c.m4a")
    assert result.mode == "linear", "a track that only needs turning down was compressed"


def test_a_measurement_from_a_failed_ffmpeg_run_is_refused(tmp_path, monkeypatch):
    # Second review of #49: the measure pass parsed loudnorm's JSON without
    # checking ffmpeg's exit. loudnorm prints its JSON even when ffmpeg stops
    # early, so a partial measurement would steer the second pass.
    partial = (
        '{"input_i" : "-30.00", "input_tp" : "-9.00", "input_lra" : "1.00",'
        ' "input_thresh" : "-40.00", "target_offset" : "0.00"}'
    )
    monkeypatch.setattr(
        eb, "_run", lambda cmd, timeout: subprocess.CompletedProcess(cmd, 1, "", partial)
    )
    with pytest.raises(eb.BakeError, match="exited 1"):
        eb.ffmpeg_measure_loudnorm(tmp_path / "m.m4a")


def test_the_masters_encoder_notes_are_not_copied(tmp_path):
    # Cloud review of #53: a lossy master's iTunSMPB (ITS encoder's priming,
    # padding and length) landed on the new AAC copy, and players that honour
    # it cut the first 48 ms of the song. The copy's own encode describes it.
    master = _master(tmp_path / "m.m4a")
    _tag(
        master,
        iTunSMPB=" 00000000 00000840 000001C0 0000000000055E00",
        ISRC="USSM16600249",
    )
    f = __import__("mutagen.mp4", fromlist=["MP4"]).MP4(master)
    f.tags["----:com.apple.iTunes:Encoding Params"] = [b"vers\x00\x00\x00\x01"]
    f.save()
    out = tmp_path / "c.m4a"
    eb.bake(master, out)
    eb.copy_tags(master, out, "car -14 LUFS master=abc")
    from mutagen.mp4 import MP4

    keys = {k.lower() for k in MP4(out).tags}
    assert "----:com.apple.itunes:itunsmpb" not in keys
    assert "----:com.apple.itunes:encoding params" not in keys
    assert "----:com.apple.itunes:isrc" in keys, "the ordinary tags still travel"
