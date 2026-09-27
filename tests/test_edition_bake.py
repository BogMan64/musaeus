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
