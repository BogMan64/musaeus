"""Review of slice A (#143): Forge's loudness tags (findings 8, 13, 14, 15)."""

from __future__ import annotations

import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mutagen.mp4 import MP4, MP4FreeForm

from musaeus.stages import forge
from musaeus.stages.base import NO_VERIFICATION

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.write_master_loudness_tags import wanted  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg")
R128 = "----:com.apple.iTunes:R128_TRACK_GAIN"
RG = "----:com.apple.iTunes:replaygain_track_gain"
PEAK = "----:com.apple.iTunes:replaygain_track_peak"


def _m4a(path: Path) -> Path:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1", "-c:a", "alac", str(path)],
                   check=True, capture_output=True)  # fmt: skip
    return path


@needs_ffmpeg
def test_forge_and_the_tag_script_agree_on_every_gain(tmp_path):
    """Finding 14: Forge's ReplayGain came from the unrounded R128 gain, the
    script's from the stored Q7.8 value; ~1 in 10 disagreed by 0.01 dB."""
    master = _m4a(tmp_path / "song.m4a")
    random.seed(7)
    for _ in range(60):
        lufs = random.uniform(-30.0, -5.0)
        assert forge._write_tags_m4a(master, -23.0 - lufs, 0.9)
        assert wanted(MP4(master).tags, lufs, {}) == {}, f"script disagrees at {lufs:.3f} LUFS"


@needs_ffmpeg
def test_a_pre_73_tag_pair_is_not_taken_as_forged(tmp_path):
    """Finding 8: R128 -5.00 dB with ReplayGain -5.00 dB (RG = R128, written
    before #73) passed the shortcut, and the song stayed 5 dB quiet."""
    master = _m4a(tmp_path / "song.m4a")
    f = MP4(master)
    f.tags[R128] = [MP4FreeForm(b"-1280")]
    f.tags[RG] = [MP4FreeForm(b"-5.00 dB")]
    f.save()
    assert not forge.rg_pair_agrees(master)
    assert forge._write_tags_m4a(master, -5.0, 0.9)
    assert forge.rg_pair_agrees(master)


@needs_ffmpeg
def test_an_unknown_peak_leaves_the_files_peak_alone(tmp_path):
    """Finding 13: --embed-from-db wrote 0.0 (silence) over a real peak."""
    master = _m4a(tmp_path / "song.m4a")
    assert forge._write_tags_m4a(master, -5.0, 0.875)
    assert forge._write_tags_m4a(master, -4.0, None)
    assert bytes(MP4(master).tags[PEAK][0]) == b"0.87500000"


def test_verify_with_nothing_tagged_this_run_says_it_did_not_look():
    """Finding 15: "found nothing to check" was reported as "checked, fine"."""
    stage = forge.ForgeStage()
    stage._tagged = []
    assert stage.verify_effect(None, None) is NO_VERIFICATION  # type: ignore[arg-type]


@needs_ffmpeg
def test_verify_reads_back_this_runs_files(tmp_path):
    master = _m4a(tmp_path / "song.m4a")
    assert forge._write_tags_m4a(master, -5.0, 0.9)
    stage = forge.ForgeStage()
    stage._tagged = [master]
    assert stage.verify_effect(None, None) == []  # type: ignore[arg-type]
