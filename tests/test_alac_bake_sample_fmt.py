"""The Lossless bake's output bit depth.

`loudnorm` outputs float, and ffmpeg's ALAC encoder then picks the widest
depth it supports rather than the one the master actually used. Nothing in
the bake command stated a sample format, so a 16-bit master baked out as
24-bit: measured 2026-08-31, 16.7 MB became 26.9 MB -- a 61% increase
carrying no additional information, because the extra 8 bits are padding.

75% of the live library is 16-bit, so unpinned this would have added
roughly 222 GB of zeros to a 486 GB edition. A "lossless" edition that is
a quarter padding is not more faithful to the master, just larger. It also
quietly invalidated the size estimator in musaeus/editions.py, which
assumes a lossless bake produces roughly the master's own size.

Third instance of the same bug class in two days, after the Car edition's
unpinned sample rate and its raw-copied noise beds: a format property
survives wherever nothing in the code path states it.

The truncation direction matters more than the inflation. Guessing s16p
for everything would silently destroy real information in a 24-bit master,
which is why an unreadable depth returns None and defers to ffmpeg rather
than assuming.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "alac_library"))
from build_alac_library import build_bake_command, source_sample_fmt  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(
    not __import__("shutil").which("ffmpeg"), reason="requires ffmpeg"
)


def _alac(path: Path, depth_fmt: str, rate: int = 44100) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration=1:sample_rate={rate}",
         "-c:a", "alac", "-sample_fmt", depth_fmt, str(path)],
        check=True, capture_output=True,
    )


def _fmt(path: Path) -> str:
    return subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_fmt", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    ).stdout.strip().rstrip(",")


class TestSourceSampleFmt:
    @needs_ffmpeg
    def test_sixteen_bit_master_reports_s16p(self, tmp_path: Path) -> None:
        f = tmp_path / "a.m4a"
        _alac(f, "s16p")
        assert source_sample_fmt(f) == "s16p"

    @needs_ffmpeg
    def test_twentyfour_bit_master_reports_s32p(self, tmp_path: Path) -> None:
        """Truncating a 24-bit master to 16 would destroy real information --
        the failure that matters far more than the inflation."""
        f = tmp_path / "b.m4a"
        _alac(f, "s32p")
        assert source_sample_fmt(f) == "s32p"

    def test_unreadable_file_defers_to_ffmpeg(self, tmp_path: Path) -> None:
        """None means 'do not state a format', not 'assume 16-bit'."""
        bad = tmp_path / "not-audio.m4a"
        bad.write_text("nonsense")
        assert source_sample_fmt(bad) is None


class TestBakeCommand:
    def _cmd(self, fmt, has_art=False):
        return build_bake_command(Path("in.m4a"), Path("out.m4a.bake_tmp"),
                                  "anull", has_art, sample_fmt=fmt)

    @pytest.mark.parametrize("has_art", [False, True])
    def test_format_is_stated_when_known(self, has_art: bool) -> None:
        cmd = self._cmd("s16p", has_art)
        assert "-sample_fmt" in cmd
        assert cmd[cmd.index("-sample_fmt") + 1] == "s16p"

    @pytest.mark.parametrize("has_art", [False, True])
    def test_nothing_stated_when_unknown(self, has_art: bool) -> None:
        assert "-sample_fmt" not in self._cmd(None, has_art)

    def test_cover_art_branch_still_copies_video(self) -> None:
        cmd = self._cmd("s16p", has_art=True)
        assert cmd[cmd.index("-c:v") + 1] == "copy"
        assert "attached_pic" in cmd


class TestRoundTrip:
    @needs_ffmpeg
    @pytest.mark.parametrize("fmt", ["s16p", "s32p"])
    def test_bake_preserves_the_master_depth(self, tmp_path: Path, fmt: str) -> None:
        """Both directions: 16-bit must not inflate, 24-bit must not truncate."""
        src = tmp_path / "src.m4a"
        _alac(src, fmt)
        out = tmp_path / "out.m4a"
        subprocess.run(
            build_bake_command(src, out, "anull", False, sample_fmt=source_sample_fmt(src)),
            check=True, capture_output=True,
        )
        assert _fmt(out) == fmt

    @needs_ffmpeg
    def test_loudnorm_is_what_widens_an_unpinned_bake(self, tmp_path: Path) -> None:
        """The defect itself, and its precise cause.

        It is NOT the ALAC encoder defaulting wide -- with a pass-through
        filter an unpinned bake stays 16-bit. It is loudnorm converting to
        float, after which the encoder follows the filter's format rather
        than the source's. The bake always runs loudnorm, which is why this
        was reachable in production and invisible in a naive test.
        """
        src = tmp_path / "src.m4a"
        _alac(src, "s16p")

        passthrough = tmp_path / "passthrough.m4a"
        subprocess.run(build_bake_command(src, passthrough, "anull", False, sample_fmt=None),
                       check=True, capture_output=True)
        assert _fmt(passthrough) == "s16p", "no filter, no widening -- not the encoder's doing"

        loud = tmp_path / "loudnorm.m4a"
        subprocess.run(
            build_bake_command(src, loud, "loudnorm=I=-18:TP=-1.0:LRA=11.0",
                               False, sample_fmt=None),
            check=True, capture_output=True,
        )
        assert _fmt(loud) != "s16p", "loudnorm should widen an unpinned bake"

        pinned = tmp_path / "pinned.m4a"
        subprocess.run(
            build_bake_command(src, pinned, "loudnorm=I=-18:TP=-1.0:LRA=11.0",
                               False, sample_fmt="s16p"),
            check=True, capture_output=True,
        )
        assert _fmt(pinned) == "s16p", "stating the format must hold it at the source depth"
