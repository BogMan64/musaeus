"""The Car edition's channel count.

Nothing stated it, so ffmpeg inherited the source's layout and three 5.1
masters -- Beck "The Paisley Experience", Billy Squier "The Big Beat",
Hoobastank "The Reason" -- shipped to the car as 5.1 AAC on 2026-09-01.
Verified in the built edition, not inferred: ffprobe reported
channels=6,5.1 on all three in _output/masked. Many head units will not
decode 5.1 AAC, and a head unit that cannot decode the stream fails on the
whole file rather than gracefully.

This is the same shape as the sample rate, in the same ffmpeg command:
an unstated format property is decided by the input, not by the target.
That makes it the fifth instance of the pattern in three days.

MONO STAYS MONO (Grey, 2026-09-02). A blanket `-ac 2` would upmix the
library's one genuinely mono master -- a 1940s Ink Spots recording -- and
invent a channel that was never recorded. So this downmixes only what has
MORE than two channels, and is a no-op for everything else.

Measured across the catalogued library: 10,440 stereo, 3 at 5.1, 1 mono.
So this changes three files and leaves 10,441 untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "car_library" / "vendor"))
from build_aac_library import build_ffmpeg_command  # noqa: E402


def _cmd(channels: int | None, *, has_art: bool = False) -> list[str]:
    return build_ffmpeg_command(
        Path("in.m4a"), Path("out.m4a"), "256k", has_art, {}, "loudnorm=x",
        44_100, channels,
    )


def _downmixes(cmd: list[str]) -> bool:
    return "-ac" in cmd and cmd[cmd.index("-ac") + 1] == "2"


@pytest.mark.parametrize("has_art", [False, True])
class TestCarChannels:
    """Both branches: the command is built differently when the source
    carries attached cover art, and the earlier fix landed in only one of
    them -- caught by testing both rather than the one in front of me."""

    @pytest.mark.parametrize("channels", [6, 8])
    def test_more_than_stereo_is_downmixed(self, channels: int, has_art: bool) -> None:
        assert _downmixes(_cmd(channels, has_art=has_art))

    def test_stereo_is_left_alone(self, has_art: bool) -> None:
        """Already the target. Stating -ac 2 would be harmless but the
        no-op keeps the command minimal and the intent readable."""
        assert not _downmixes(_cmd(2, has_art=has_art))

    def test_mono_stays_mono(self, has_art: bool) -> None:
        """The rule, explicitly. Upmixing invents a channel that was never
        recorded -- for the Ink Spots that means fabricating stereo for a
        1940s mono master."""
        assert not _downmixes(_cmd(1, has_art=has_art))

    def test_an_unreadable_channel_count_changes_nothing(self, has_art: bool) -> None:
        """Same posture as probe_sample_rate: when the source cannot be
        read, leave it alone rather than guess. Guessing here would upmix
        or downmix on no evidence."""
        assert not _downmixes(_cmd(None, has_art=has_art))


def test_the_sample_rate_pin_still_applies_alongside_it() -> None:
    """The two properties are stated in the same command; adding one must
    not displace the other."""
    cmd = _cmd(6)
    assert "-ar" in cmd and cmd[cmd.index("-ar") + 1] == "44100"
    assert _downmixes(cmd)
