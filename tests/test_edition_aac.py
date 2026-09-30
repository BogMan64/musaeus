"""The AAC editions' encode (edition_bake.bake_aac): car and iPhone.

One ffmpeg pass from the master (Grey, 2026-09-28). Real ffmpeg on synthetic
masters, and the files measured -- rate, channels, codec, loudness, peaks:
four format bugs in three days were visible only by probing the output.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus import edition_bake as eb

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe") and shutil.which("fdkaac")),
    reason="ffmpeg/ffprobe/fdkaac not available",
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
    # Nothing reads the terminal: ffmpeg's stdin is closed, and fdkaac's is
    # the pipe from ffmpeg.
    assert seen and all(stdin is not None for _, stdin in seen), seen
    assert all(stdin == subprocess.DEVNULL for cmd, stdin in seen if cmd[0] == "ffmpeg")
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
        last = graph.rsplit("]", 2)[-2] if noise else graph
        assert f"alimiter=limit={eb.CEILING}:level=disabled" in last, graph


def test_the_finished_file_is_measured_not_only_the_report(tmp_path, monkeypatch):
    # Cloud review of #53 (CLAUDE.md: measure the artifact, not the report):
    # loudnorm reports the music BEFORE the noise, the limiter and the AAC
    # encode. With the noise far too loud the report still said -14.
    monkeypatch.setattr(eb, "NOISE_LEVELS_DB", {"brown": 8.0, "pink": 8.0, "white": 8.0})
    with pytest.raises(eb.BakeError, match="finished copy"):
        eb.bake_aac(_master(tmp_path / "m.m4a"), tmp_path / "car.m4a", noise=True)


def test_a_copy_a_fifth_of_a_second_short_is_refused(tmp_path, monkeypatch):
    # Cloud review of #53: with ffmpeg 6.1 the noise mix cut ~0.2 s off 48 kHz
    # car copies, inside the shared 2 s / 2 % rule. Not on ffmpeg 5.1 here --
    # so the AAC copies are held to 0.1 s, and an upgrade cannot hide it.
    master = _master(tmp_path / "m.m4a", seconds=20)
    out = tmp_path / "car.m4a"
    real = eb.probe

    def short(path):
        info = real(path)
        if Path(path) == out:
            info["format"]["duration"] = str(float(info["format"]["duration"]) - 0.2)
        return info

    monkeypatch.setattr(eb, "probe", short)
    with pytest.raises(eb.BakeError, match="length changed"):
        eb.bake_aac(master, out, noise=True)


def test_the_time_limit_grows_with_the_song(tmp_path, monkeypatch):
    # Cloud review of #53: a flat 30-minute working-time limit would kill a
    # multi-hour master's encode on every build. Here the flat limit is made
    # smaller than a 12 s song; every ffmpeg call must still get the song's
    # length at least.
    master = _master(tmp_path / "m.m4a", seconds=12)
    monkeypatch.setattr(eb, "_BAKE_TIMEOUT", 5)
    limits = []
    real = eb._run

    def spy(cmd, timeout):
        if cmd[0] == eb.FFMPEG:
            limits.append(timeout)
        return real(cmd, timeout)

    monkeypatch.setattr(eb, "_run", spy)
    eb.bake_aac(master, tmp_path / "car.m4a", noise=True)
    assert limits and min(limits) >= 12, limits


def test_a_truncated_master_is_refused_not_padded(tmp_path):
    # The container says 30 s; the audio stops short. The mix must end where
    # the music ends, so the copy is refused -- never filled out with noise.
    master = tmp_path / "m.m4a"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=15:sample_rate=44100",
         "-f", "lavfi", "-i", "sine=frequency=660:duration=15:sample_rate=44100",
         "-filter_complex", "[0]volume=-4dB[a];[1]volume=-9dB[b];[a][b]concat=n=2:v=0:a=1",
         "-ac", "2", "-c:a", "alac", "-movflags", "+faststart", str(master)],
        check=True,
    )  # fmt: skip
    data = master.read_bytes()
    master.write_bytes(data[: int(len(data) * 0.94)])
    assert float(eb.probe(master)["format"]["duration"]) == pytest.approx(30.0, abs=0.1)
    with pytest.raises(eb.BakeError, match="length changed"):
        eb.bake_aac(master, tmp_path / "car.m4a", noise=True)


def _hidden_track_master(path: Path) -> Path:
    """A song, a long silence, then a hidden track: half the file is silence."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=20:sample_rate=44100",
         "-f", "lavfi", "-i", "sine=frequency=660:duration=20:sample_rate=44100",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=60",
         "-f", "lavfi", "-i", "sine=frequency=550:duration=20:sample_rate=44100",
         "-filter_complex",
         "[0]volume=-4dB[a];[1]volume=-9dB[b];[3]volume=-6dB[d];"
         "[a][b][2][d]concat=n=4:v=0:a=1,pan=stereo|c0=c0|c1=c0",
         "-c:a", "alac", "-sample_fmt", "s32p", str(path)],
        check=True,
    )  # fmt: skip
    return path


def test_a_hidden_track_gets_its_car_copy(tmp_path):
    # Cloud review of #53: the noise fills the silence, which the loudness
    # gate then counts, so the finished car file measured ~2 LU under the
    # music and a correct copy was refused on every build.
    master = _hidden_track_master(tmp_path / "m.m4a")
    result = eb.bake_aac(master, tmp_path / "car.m4a", noise=True)
    assert result.achieved_lufs is not None


def test_a_car_copy_with_no_noise_in_it_is_refused(tmp_path, monkeypatch):
    # ...and the check could not tell a car copy with no noise at all. The
    # noise is dropped from the graph while the build still expects it (a
    # graph fault); turning the levels down would move the expected bed too.
    real = eb.aac_filter

    def no_noise(loudnorm, rate, channels, seconds, noise, ceiling=None, **kw):
        return real(loudnorm, rate, channels, seconds, False, ceiling, **kw)

    monkeypatch.setattr(eb, "aac_filter", no_noise)
    master = _hidden_track_master(tmp_path / "m.m4a")
    with pytest.raises(eb.BakeError, match="noise"):
        eb.bake_aac(master, tmp_path / "car.m4a", noise=True)


@pytest.mark.parametrize(
    "source,expected", [(37_800, 44_100), (44_056, 44_100), (47_250, 48_000), (22_050, 22_050)]
)
def test_an_odd_rate_goes_up_to_the_next_rate_aac_takes(source, expected):
    # Cloud review of #53: the AAC encoder takes only standard rates, and a
    # master at 37.8, 44.056 or 47.25 kHz failed the encode on every build.
    assert eb.target_rate(source) == expected


def test_an_odd_rate_master_encodes(tmp_path):
    master = _master(tmp_path / "m.m4a", rate=47_250)
    eb.bake_aac(master, tmp_path / "c.m4a", noise=True)
    assert eb.sample_rate_of(eb.probe(tmp_path / "c.m4a")) == 48_000


def _limits_used(monkeypatch) -> list[float]:
    seen: list[float] = []
    real = eb._run

    def spy(cmd, timeout):
        graph = next((c for c in cmd if "alimiter=limit=" in c), "")
        seen.extend(float(x) for x in re.findall(r"alimiter=limit=([\d.]+)", graph))
        return real(cmd, timeout)

    real_pipe = eb._run_pipeline

    def spy_pipe(first, second, timeout):
        graph = next((c for c in first if "alimiter=limit=" in c), "")
        seen.extend(float(x) for x in re.findall(r"alimiter=limit=([\d.]+)", graph))
        return real_pipe(first, second, timeout)

    monkeypatch.setattr(eb, "_run", spy)
    monkeypatch.setattr(eb, "_run_pipeline", spy_pipe)
    return seen


def test_a_copy_that_peaks_over_is_encoded_again_with_a_lower_limit(tmp_path, monkeypatch):
    # The 200-song vault build, 2026-09-29: 11 car copies peaked over 0 dBTP
    # after the AAC encode, one at +2.1 (The Commitments' "In The Midnight
    # Hour": -0.2 before the encoder, +2.1 after). Grey: fix it -- measure
    # the peak, and when it is over, encode again with the limiter lowered by
    # the overshoot.
    real = eb.loudness_of
    peaks = iter([2.1, -0.6])

    def overshoot(path, timeout=None):
        i, quiet, _ = real(path, timeout)
        return i, quiet, next(peaks)

    monkeypatch.setattr(eb, "loudness_of", overshoot)
    limits = _limits_used(monkeypatch)
    eb.bake_aac(_master(tmp_path / "m.m4a"), tmp_path / "car.m4a", noise=True)
    assert len(limits) == 2, limits
    lowered_db = 20 * math.log10(limits[1] / limits[0])
    assert lowered_db == pytest.approx(-(2.1 - eb._PEAK_AIM_DBTP), abs=0.05), lowered_db


def test_a_copy_still_far_over_after_the_tries_is_refused(tmp_path, monkeypatch):
    real = eb.loudness_of

    def always_over(path, timeout=None):
        i, quiet, _ = real(path, timeout)
        return i, quiet, 3.0

    monkeypatch.setattr(eb, "loudness_of", always_over)
    with pytest.raises(eb.BakeError, match="peak"):
        eb.bake_aac(_master(tmp_path / "m.m4a"), tmp_path / "car.m4a", noise=True)


def test_a_copy_within_the_limit_is_encoded_once(tmp_path, monkeypatch):
    limits = _limits_used(monkeypatch)
    eb.bake_aac(_master(tmp_path / "m.m4a"), tmp_path / "car.m4a", noise=True)
    assert limits == [eb.CEILING]


def test_the_aac_copy_is_encoded_by_fdk(tmp_path, monkeypatch):
    # 2026-09-30: ffmpeg's own AAC encoder (5.1 and 6.1 alike) put short
    # spikes into ~4% of songs -- Melissa Etheridge's "I Want To Come Over"
    # peaks -13.1 dBFS and a plain encode spiked to -5.3; RMS unchanged, so
    # clicks. Fraunhofer's FDK (fdkaac) gave -13.7. Grey: switch.
    seen = []
    real = subprocess.Popen

    def spy(cmd, *a, **k):
        seen.append(list(cmd))
        return real(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", spy)
    eb.bake_aac(_master(tmp_path / "m.m4a"), tmp_path / "car.m4a", noise=True)
    monkeypatch.undo()
    assert any(Path(cmd[0]).name == "fdkaac" for cmd in seen), "not encoded by fdkaac"
    assert not any("-c:a" in cmd and cmd[cmd.index("-c:a") + 1] == "aac" for cmd in seen)
    assert "enc=fdkaac" in eb.aac_settings(noise=True), "a copy made before must be made again"
    assert eb._audio_stream(eb.probe(tmp_path / "car.m4a"))["codec_name"] == "aac"


def test_the_copy_keeps_its_own_gapless_note_not_the_masters(tmp_path):
    # fdkaac writes the copy's own encoder delay and padding (iTunSMPB) for
    # gapless playback; copy_tags must keep it, and never put the master's
    # in its place (cloud review of #53, finding 6: a player honouring the
    # master's cut 48 ms off the start).
    from mutagen.mp4 import MP4, MP4FreeForm

    master = _master(tmp_path / "m.m4a")
    f = MP4(master)
    if f.tags is None:
        f.add_tags()
    f.tags["----:com.apple.iTunes:iTunSMPB"] = [MP4FreeForm(b" 00000000 00000840 000001C0 0")]
    f.save()
    out = tmp_path / "phone.m4a"
    eb.bake_aac(master, out, noise=False)
    own = MP4(out).tags["----:com.apple.iTunes:iTunSMPB"]
    eb.copy_tags(master, out, "iphone -14.0 LUFS master=abc")
    after = MP4(out).tags["----:com.apple.iTunes:iTunSMPB"]
    assert after == own and bytes(after[0]) != b" 00000000 00000840 000001C0 0"


def test_a_click_the_encoder_adds_is_refused(tmp_path, monkeypatch):
    # 2026-09-30: ffmpeg's encoder put clicks into ~4% of songs, and the peak
    # check caught only those crossing 0 dBTP (Melissa Etheridge's peaked at
    # -5.3 over music at -13). Every 100 ms of the copy is now compared with
    # the audio that went into the encoder; +3 dB or more is a click.
    real = eb.window_peaks

    def clicked(path, rate, timeout=None):
        peaks = real(path, rate, timeout)
        if Path(path).suffix == ".m4a" and len(peaks) > 20:
            peaks[20] += 6.0  # one 100 ms stretch, 6 dB over what went in
        return peaks

    monkeypatch.setattr(eb, "window_peaks", clicked)
    with pytest.raises(eb.BakeError, match="click"):
        eb.bake_aac(_master(tmp_path / "m.m4a"), tmp_path / "car.m4a", noise=True)
