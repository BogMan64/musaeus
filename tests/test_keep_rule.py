"""Grey's keep rule, 2026-10-03, reordered 2026-10-22: studio over live above
all, then lossless (ALAC and FLAC equal) over lossy, original over remaster,
then sample rate, then length (2 s or more); a tie keeps the library copy."""

from __future__ import annotations

import pytest

from musaeus.keep_rule import decide

BASE = {"codec": "alac", "title": "Song", "album": "Album", "sample_rate": 44100, "duration": 200.0}


def c(**kw):
    return {**BASE, **kw}


# A live copy has a length of its own: a copy at a live copy's length is that live
# take (batch 7, 2026-10-25; test_one_live_take_is_live_on_every_copy below).
@pytest.mark.parametrize(
    ("review", "library", "want"),
    [
        (c(codec="alac"), c(codec="flac"), ("tie", "")),
        (c(codec="flac", sample_rate=192000), c(), ("review", "quality")),
        # Linda Ronstadt, "Somewhere Out There": the 3:58 FLAC arrival lost to the
        # 3:07 ALAC library copy on format before length was asked (2026-10-22)
        (c(codec="flac", duration=238.2), c(duration=186.5), ("review", "length")),
        (
            c(codec="aac"),
            c(codec="flac", title="Song (Live)", duration=230.0),
            ("review", "studio/live"),
        ),
        (c(codec="aac"), c(codec="flac"), ("library", "format")),
        (
            c(title="Song (Live)", sample_rate=192000, duration=230.0),
            c(),
            ("library", "studio/live"),
        ),
        (c(), c(album="Unplugged", duration=230.0), ("review", "studio/live")),
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


def test_one_live_take_is_live_on_every_copy():
    # Bruce Hornsby "The End Of The Innocence": the 192 kHz "(Live)" original and its
    # "(Remastered 2003)" copy, 438.7 s both; only the original says "Live" (batch 7).
    live = c(title="Song (Live)", sample_rate=192000, duration=438.7)
    remaster = c(title="Song (Remastered 2003)", duration=438.7)
    assert decide(live, remaster) == ("review", "original/remaster")


def test_bitrate_is_not_quality():
    """Two ALAC copies of one recording differ in bitrate by noise; a swap on
    that remakes every edition copy for nothing (AC/DC "Big Gun", 2026-10-03)."""
    assert decide(c(bitrate=1_100_000), c(bitrate=900_000)) == ("tie", "")
