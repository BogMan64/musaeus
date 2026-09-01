"""The Car edition's output sample rate.

Nothing pinned it, so ffmpeg's AAC encoder capped at its own maximum: a
192 kHz master came out as 96 kHz AAC. 44.1 and 48 kHz AAC-LC are
supported essentially everywhere; above 48 support is patchy, and a head
unit that cannot decode the stream fails on the whole file rather than
gracefully.

Measured on the live library 2026-08-31: 4,862 of 10,545 catalogued files
(46%) are above 48 kHz, 4,223 of them at 192 kHz. Caught by verifying a
3-track test build instead of trusting that it "completed".

Capped, not forced -- forcing 48 would resample the 5,439 files already at
44.1 kHz (52%) at a non-integer ratio for no gain.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "car_library" / "vendor"))
from build_aac_library import build_ffmpeg_command, car_sample_rate  # noqa: E402


class TestCarSampleRate:
    @pytest.mark.parametrize("rate", [8_000, 22_050, 44_100, 48_000])
    def test_at_or_below_48k_is_pinned_to_itself_not_changed(self, rate: int) -> None:
        """Half the library is already 44.1 -- resampling it gains nothing,
        but the rate must still be STATED.

        ffmpeg's loudnorm filter resamples internally and emits at its own
        rate, so an encode with no -ar takes the FILTER's rate rather than
        the source's. Measured 2026-08-31: a 44,100 Hz master came out as
        96,000 Hz AAC through the loudnorm chain with no downsample involved.
        Returning None here (the first version of this fix) left every file
        at or below 48k exposed to exactly that.
        """
        assert car_sample_rate(rate) == rate

    @pytest.mark.parametrize("source,expected", [
        (192_000, 48_000),   # /4  -- 40% of the library
        (96_000, 48_000),    # /2
        (88_200, 44_100),    # /2  -- stays in the 44.1 family
        (176_400, 44_100),   # /4  -- stays in the 44.1 family
    ])
    def test_downsampled_within_its_own_clock_family(self, source, expected) -> None:
        assert car_sample_rate(source) == expected

    def test_ratio_is_always_an_exact_power_of_two(self) -> None:
        """Crossing families would resample at 160/147 instead of /2 or /4."""
        for source in (88_200, 96_000, 176_400, 192_000):
            target = car_sample_rate(source)
            assert source % target == 0, f"{source} -> {target} is not an integer ratio"
            assert (source // target) in (2, 4)

    def test_unreadable_rate_is_left_alone(self) -> None:
        """A probe that fails must not silently force a resample."""
        assert car_sample_rate(None) is None
        assert car_sample_rate(0) is None

    def test_every_readable_rate_produces_an_explicit_ar(self) -> None:
        """The property that actually protects the encode: whatever the
        source, the command states a rate rather than inheriting one."""
        for rate in (22_050, 44_100, 48_000, 88_200, 96_000, 176_400, 192_000):
            assert car_sample_rate(rate) is not None
            assert car_sample_rate(rate) <= 48_000


class TestFfmpegCommand:
    def _cmd(self, rate, has_pic=False):
        return build_ffmpeg_command(
            input_file=Path("in.flac"), output_file=Path("out.m4a"),
            bitrate="256k", has_attached_picture=has_pic,
            clean_tags={}, loudnorm_filter="anull", target_rate=rate,
        )

    @pytest.mark.parametrize("has_pic", [False, True])
    def test_ar_is_emitted_when_capping(self, has_pic: bool) -> None:
        cmd = self._cmd(48_000, has_pic)
        assert "-ar" in cmd
        assert cmd[cmd.index("-ar") + 1] == "48000"

    @pytest.mark.parametrize("has_pic", [False, True])
    def test_no_ar_when_nothing_to_cap(self, has_pic: bool) -> None:
        """A 44.1 source must produce a command with no -ar at all."""
        assert "-ar" not in self._cmd(None, has_pic)

    def test_cover_art_branch_still_copies_video(self) -> None:
        """The -ar insertion must not disturb the attached-picture mapping."""
        cmd = self._cmd(48_000, has_pic=True)
        assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "copy"
        assert "attached_pic" in cmd
