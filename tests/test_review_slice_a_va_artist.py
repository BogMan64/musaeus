"""Review of slice A (#143): VariousArtistsFix.

Finding 3: the bracket strategy took any [...] in a file name as the real
artist, and ran before the "Various Artists - Artist - Title" rule, so
"[Live]", "[Remastered 2009]" or "[Explicit]" became the artist and the master
was moved under it. And its MusicBrainz lookup called urlopen without asking
the network gateway.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus import network_policy
from musaeus.network_policy import NetworkPolicy
from musaeus.stages import various_artists_fix as va


def _artist(name: str) -> tuple[str, str]:
    return va.find_real_artist(Path(name), title="", album="", use_mb=False)


@pytest.mark.parametrize(
    "name",
    [
        "Various Artists - Song [Remastered 2009].m4a",
        "Various Artists - Song [Live].m4a",
        "Various Artists - Song [Explicit].m4a",
        "Various Artists - Song [Mono].m4a",
        "Various Artists - Song [2019 Mix].m4a",
        "Various Artists - Song [Bonus Track].m4a",
        "Various Artists - Song [Live at Wembley].m4a",
        "Various Artists - Song [50th Anniversary Edition].m4a",
        # Grey's code review, 2026-10-11, finding 7.
        "Various Artists - Song [Official Video].m4a",
        "Various Artists - Song [Official Music Video].m4a",
        "Various Artists - Song [feat. Drake].m4a",
        "Various Artists - Song [ft. Rihanna].m4a",
        "Various Artists - Song [Club Mix].m4a",
        "Various Artists - Song [Tiesto Remix].m4a",
        "Various Artists - Song [Long Version].m4a",
        "Various Artists - Song [Kygo Rework].m4a",
        "Various Artists - Song [Prod. by Metro Boomin].m4a",
        "Various Artists - Song [featuring Drake].m4a",
        # Pre-merge review of #185.
        "Various Artists - Song [With The Royal Philharmonic Orchestra].m4a",
        "Various Artists - Song [w/ Drake].m4a",
        "Various Artists - Song [Sped Up].m4a",
        "Various Artists - Song [Official 4K Video].m4a",
        "Various Artists - Song [12'' Mix].m4a",
        "Various Artists - Song [VIP Mix].m4a",
        "Various Artists - Song [Re-Edit].m4a",
        "Various Artists - Song [MTV Unplugged].m4a",
        "Various Artists - Song [Soundtrack].m4a",
        "Various Artists - Song [Lyric Video].m4a",
    ],
)
def test_a_description_of_the_recording_is_not_an_artist(name):
    assert _artist(name) == ("", "unknown")


def test_the_filename_rule_is_read_before_a_bracket():
    name = "Various Artists - Al Green - Let's Stay Together [Live].m4a"
    assert _artist(name) == ("Al Green", "filename_segment")


@pytest.mark.parametrize(
    ("name", "artist"),
    [
        ("Various Artists - Be My Baby [The Ronettes-Phil Spector].m4a", "The Ronettes"),
        ("Various Artists - Wings [Little Mix].m4a", "Little Mix"),
        ("Various Artists - Song [Club Nouveau].m4a", "Club Nouveau"),
        ("Various Artists - Song [Audio Adrenaline].m4a", "Audio Adrenaline"),
        ("Various Artists - Song [The Music].m4a", "The Music"),
        ("Various Artists - Song [Live Wire Band].m4a", ""),  # refused, not guessed
    ],
)
def test_a_real_artist_in_brackets_is_still_read(name, artist):
    assert _artist(name)[0] == artist


def test_the_musicbrainz_lookup_asks_the_gateway(monkeypatch):
    def no_transport(*a, **k):
        raise AssertionError("urlopen was called under LOCAL_ONLY")

    monkeypatch.setattr(va, "urlopen", no_transport)
    with network_policy.policy(NetworkPolicy.LOCAL_ONLY) as gateway:
        assert va._lookup_musicbrainz("Be My Baby") == ""
    assert gateway.denials, "the lookup never asked the gateway"


def test_a_band_named_with_digits_is_read():
    assert _artist("Various Artists - Song [10cc].m4a") == ("10cc", "bracket")
