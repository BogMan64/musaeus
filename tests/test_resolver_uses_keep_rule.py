"""The duplicate resolver keeps the copy Grey's keep rule keeps (Grey, 2026-10-05).

Planned on the live vault before any move: the resolver ranked by bitrate and size,
so 32 of 256 moves kept the shorter copy and 3 the lower sample rate. Grey: "you
may change the keep rule". One rule now: format, an original over an old baked
copy, studio over live, original over remaster, sample rate, the longer copy when
2 s or more longer, then the copy already filed.
"""

from __future__ import annotations

from musaeus.stages.dupe_resolver import _rank


def _m(name, **kw):
    m = {"file_path": name, "codec": "alac", "title": "Song", "album": "Album",
         "sample_rate": 44100, "duration": 200.0, "bitrate": 1000, "size_bytes": 1000,
         "lufs": -12.0, "finalized_at": "x"}  # fmt: skip
    m.update(kw)
    return m


def _keeper(*ms):
    members = list(ms)
    _rank(members)
    return members[0]["file_path"]


def test_the_longer_copy_wins_when_quality_ties():
    # Steve Winwood "Higher Love": 348.7 s and 351.5 s, both 192 kHz; bitrate kept the shorter
    short = _m("short", sample_rate=192000, duration=348.7, bitrate=4_000_000)
    long_ = _m("long", sample_rate=192000, duration=351.5, bitrate=3_000_000)
    assert _keeper(short, long_) == "long"


def test_lengths_under_2_seconds_apart_are_the_same_length():
    a = _m("in_place", duration=200.0)
    b = _m("new", duration=201.5, finalized_at=None)
    assert _keeper(b, a) == "in_place"


def test_sample_rate_decides_not_bitrate():
    # Eric Clapton "Miles Road": 44.1 kHz at 1.26 Mb/s against 48 kHz at 0.89 Mb/s
    lo = _m("44k", sample_rate=44100, bitrate=1_262_235)
    hi = _m("48k", sample_rate=48000, bitrate=885_798)
    assert _keeper(lo, hi) == "48k"


def test_an_original_beats_an_old_baked_copy_even_at_a_higher_sample_rate():
    # Handel "Lascia Ch'io Pianga": 192 kHz at -18.01 LUFS (baked) against the 44.1 kHz original
    baked = _m("baked", sample_rate=192000, lufs=-18.01)
    original = _m("original", sample_rate=44100, lufs=-25.35)
    assert _keeper(baked, original) == "original"


def test_lossless_beats_lossy_whatever_the_length():
    alac = _m("alac", duration=259.8, sample_rate=48000)
    aac = _m("aac", codec="aac", duration=281.3, sample_rate=96000)
    assert _keeper(aac, alac) == "alac"


def test_an_unknown_codec_is_not_taken_for_lossless():
    unknown = _m("unknown", codec=None, sample_rate=192000)
    alac = _m("alac")
    assert _keeper(unknown, alac) == "alac"


def test_studio_then_remaster_then_live():
    # Grey, 2026-10-05: "Studio is above remaster. Live would be the third option."
    studio = _m("studio", title="Song")
    remaster = _m("remaster", title="Song (Remastered)")
    live = _m("live", title="Song (Live)")
    members = [live, remaster, studio]
    _rank(members)
    assert [m["file_path"] for m in members] == ["studio", "remaster", "live"]
