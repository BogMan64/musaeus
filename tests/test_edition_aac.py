"""The AAC editions' encode (edition_bake.bake_aac): car and iPhone.

One ffmpeg pass from the master (Grey, 2026-09-28). Real ffmpeg on synthetic
masters, and the files measured -- rate, channels, codec, loudness, peaks:
four format bugs in three days were visible only by probing the output.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus import edition_bake as eb

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not available",
)


def _master(path: Path, *, rate: int = 44_100, layout: str = "stereo", seconds: int = 12) -> Path:
    """Two levels, so loudnorm can measure a range (a steady tone's is 0)."""
    half = seconds / 2
    pan = {
        "mono": "mono|c0=c0",
        "stereo": "stereo|c0=c0|c1=c0",
        "5.1": "5.1|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|c5=c0",
    }[layout]
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={half}:sample_rate={rate}",
         "-f", "lavfi", "-i", f"sine=frequency=660:duration={half}:sample_rate={rate}",
         "-filter_complex",
         f"[0]volume=-4dB[a];[1]volume=-9dB[b];[a][b]concat=n=2:v=0:a=1,pan={pan}",
         "-c:a", "alac", "-sample_fmt", "s32p", str(path)],
        check=True,
    )  # fmt: skip
    return path


def _measure(path: Path) -> tuple[float, float]:
    err = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(path),
         "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    ).stderr  # fmt: skip
    i = float(re.findall(r"I:\s+(-?\d+\.\d) LUFS", err)[-1])
    tp = float(re.findall(r"Peak:\s+(-?\d+\.\d) dBFS", err)[-1])
    return i, tp


@pytest.mark.parametrize("colour", ["brown", "pink", "white"])
def test_each_noise_colour_is_calibrated_to_the_old_beds(colour):
    # The old beds were each colour at -16 LUFS; Grey's levels apply on top.
    gain = eb.NOISE_BED_LUFS - eb.NOISE_RAW_LUFS[colour]
    err = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-f", "lavfi",
         "-i", f"anoisesrc=colour={colour}:duration=30:sample_rate=48000:seed=7",
         "-af", f"volume={gain:.2f}dB,ebur128", "-f", "null", "-"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    ).stderr  # fmt: skip
    i = float(re.findall(r"I:\s+(-?\d+\.\d) LUFS", err)[-1])
    assert abs(i - eb.NOISE_BED_LUFS) <= 0.5, (colour, i)


@pytest.mark.parametrize(
    "source,expected",
    [(44_100, 44_100), (48_000, 48_000), (32_000, 32_000), (88_200, 44_100),
     (96_000, 48_000), (176_400, 44_100), (192_000, 48_000), (None, None)],
)  # fmt: skip
def test_the_rate_is_the_masters_up_to_48k_then_its_own_family(source, expected):
    assert eb.target_rate(source) == expected


def test_a_car_copy_is_one_aac_pass_with_the_noise_under_it(tmp_path):
    master = _master(tmp_path / "m.m4a", rate=96_000)
    out = tmp_path / "car.m4a"
    result = eb.bake_aac(master, out, noise=True)
    info = eb.probe(out)
    assert eb._audio_stream(info)["codec_name"] == "aac"
    assert eb.sample_rate_of(info) == 48_000, "96 kHz comes down to 48 kHz"
    assert eb.channels_of(info) == 2
    assert result.achieved_lufs is not None and abs(result.achieved_lufs + 14.0) <= 1.0
    finished, peak = _measure(out)
    assert finished > result.achieved_lufs, "the noise was not mixed in"
    assert peak <= 0.0, f"the limiter let a peak through: {peak}"


def test_a_5_1_master_comes_out_stereo_and_mono_stays_mono(tmp_path):
    surround = eb.bake_aac(
        _master(tmp_path / "s.m4a", layout="5.1"), tmp_path / "s_car.m4a", noise=True
    )
    mono = eb.bake_aac(
        _master(tmp_path / "m.m4a", layout="mono"), tmp_path / "m_car.m4a", noise=True
    )
    assert surround.achieved_lufs is not None and mono.achieved_lufs is not None
    assert eb.channels_of(eb.probe(tmp_path / "s_car.m4a")) == 2
    assert eb.channels_of(eb.probe(tmp_path / "m_car.m4a")) == 1


def test_an_iphone_copy_is_the_same_without_noise(tmp_path):
    master = _master(tmp_path / "m.m4a", rate=44_100)
    out = tmp_path / "phone.m4a"
    result = eb.bake_aac(master, out, noise=False)
    info = eb.probe(out)
    assert eb._audio_stream(info)["codec_name"] == "aac" and eb.sample_rate_of(info) == 44_100
    finished, _ = _measure(out)
    assert abs(finished + 14.0) <= 1.0, "without noise the file itself lands on -14"
    assert result.achieved_lufs is not None


def test_the_aac_encode_runs_with_stdin_closed(tmp_path, monkeypatch):
    seen = []
    real = subprocess.Popen

    def spy(cmd, *a, **k):
        seen.append((list(cmd), k.get("stdin")))
        return real(cmd, *a, **k)

    master = _master(tmp_path / "m.m4a")
    monkeypatch.setattr(subprocess, "Popen", spy)
    eb.bake_aac(master, tmp_path / "c.m4a", noise=True)
    monkeypatch.undo()
    assert seen and all(stdin == subprocess.DEVNULL for _, stdin in seen)
    assert all("-nostdin" in cmd for cmd, _ in seen if cmd[0] == "ffmpeg")


@pytest.mark.parametrize("noise", [True, False])
def test_a_master_with_cover_art_keeps_its_length_and_its_art(tmp_path, noise):
    # Rehearsal, 2026-09-28: Martha Reeves' "A Love Like Yours" (cover art in
    # the master) came out 0.09 s long. Bisected: the in-graph noise sources
    # plus the art mapped as a second stream end the file at once.
    from mutagen.mp4 import MP4, MP4Cover

    master = _master(tmp_path / "m.m4a")
    jpeg = tmp_path / "cover.jpg"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "color=c=red:s=64x64", "-frames:v", "1", str(jpeg)],
        check=True,
    )  # fmt: skip
    f = MP4(master)
    if f.tags is None:
        f.add_tags()
    f.tags["covr"] = [MP4Cover(jpeg.read_bytes(), imageformat=MP4Cover.FORMAT_JPEG)]
    f.save()
    assert eb.has_attached_picture(eb.probe(master))  # the case, not an easy one

    out = tmp_path / "c.m4a"
    eb.bake_aac(master, out, noise=noise)
    eb.copy_tags(master, out, "car -14 LUFS master=abc")
    assert abs(float(eb.probe(out)["format"]["duration"]) - 12.0) < 0.5
    assert MP4(out).tags["covr"][0] == jpeg.read_bytes(), "the copy lost its art"


def test_a_5_1_copy_plays_at_the_loudness_it_reports(tmp_path):
    # loudnorm measured the six channels, then the fold to stereo changed the
    # loudness: a synthetic 5.1 master reported -13.5 and played at -10.5.
    # The fold (and the rate) now come BEFORE both loudnorm passes, so what
    # is measured is what is encoded.
    out = tmp_path / "phone.m4a"
    result = eb.bake_aac(_master(tmp_path / "s.m4a", layout="5.1"), out, noise=False)
    finished, _ = _measure(out)
    assert result.achieved_lufs is not None
    assert abs(finished - result.achieved_lufs) <= 0.5, (finished, result.achieved_lufs)
    assert abs(finished + 14.0) <= 1.0, finished


def test_the_iphone_copy_is_limited_too():
    # Rehearsal, 2026-09-28: Spirit Of The West's iPhone copy peaked at
    # +1.9 dBTP (its car copy, limited, -0.4); with the limiter, -0.8.
    # Synthetic masters never overshot, so the graph itself is checked.
    for noise in (True, False):
        graph = eb.aac_filter("loudnorm=linear=true", 44_100, 2, 10.0, noise)
        assert graph.endswith(f"alimiter=limit={eb.CEILING}:level=disabled[out]"), graph
