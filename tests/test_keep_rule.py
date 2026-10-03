"""Grey's keep rule, 2026-10-03: ALAC over FLAC, studio over live, original
over remaster, then sample rate, then length (2 s or more); a tie keeps the
library copy."""

from __future__ import annotations

import pytest

from musaeus.keep_rule import decide

BASE = {"codec": "alac", "title": "Song", "album": "Album", "sample_rate": 44100, "duration": 200.0}


def c(**kw):
    return {**BASE, **kw}


@pytest.mark.parametrize(
    ("review", "library", "want"),
    [
        (c(codec="alac"), c(codec="flac"), ("review", "format")),
        (c(codec="flac", sample_rate=192000), c(), ("library", "format")),
        (c(codec="aac"), c(codec="flac"), ("library", "format")),
        (c(title="Song (Live)", sample_rate=192000), c(), ("library", "studio/live")),
        (c(), c(album="Unplugged"), ("review", "studio/live")),
        (c(title="Song (Remastered)", sample_rate=96000), c(), ("library", "original/remaster")),
        (c(), c(title="Song (2016 Remix)"), ("review", "original/remaster")),
        (c(sample_rate=192000), c(), ("review", "quality")),
        (c(duration=202.0), c(), ("review", "length")),
        (c(duration=201.9), c(), ("tie", "")),
        (c(duration=197.5), c(), ("library", "length")),
        (c(), c(), ("tie", "")),
    ],
)
def test_the_order_grey_ruled(review, library, want):
    assert decide(review, library) == want


def test_bitrate_is_not_quality():
    """Two ALAC copies of one recording differ in bitrate by noise; a swap on
    that remakes every edition copy for nothing (AC/DC "Big Gun", 2026-10-03)."""
    assert decide(c(bitrate=1_100_000), c(bitrate=900_000)) == ("tie", "")
