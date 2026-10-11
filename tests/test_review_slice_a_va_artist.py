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
