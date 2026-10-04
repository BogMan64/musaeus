"""A playlist's name is not an album: Act 1 drops it, the tagger removes the tag,
and a one-off clears the ones already in the library (Grey, 2026-10-03)."""

from __future__ import annotations

import shutil
import subprocess

import pytest
from mutagen.mp4 import MP4

from musaeus.playlist_album import is_playlist_album
from musaeus.stages.tagger import TaggerStage, _read_tags, _write_tags


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("My playlist S", True),
        ("my playlist", True),
        ("MY PLAYLIST Z", True),
        ("  My Playlist 3 ", True),
        ("60's British Invasion Playlist", True),
        ("Playlist: The Very Best of ABBA", False),
        ("Playlist Plus", False),
        ("Playlists of Fire", False),
        ("Toto IV", False),
        ("", False),
        (None, False),
    ],
)
def test_what_counts_as_a_playlist_name(name, expected):
    assert is_playlist_album(name) is expected


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")


@pytest.fixture
def m4a(tmp_path):
    p = tmp_path / "s.m4a"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=1", "-c:a", "alac", str(p)],
        check=True,
        capture_output=True,
    )
    audio = MP4(str(p))
    audio["\xa9alb"] = ["My playlist S"]
    audio["\xa9nam"] = ["Song"]
    audio.save()
    return p


def _row(album):
    return {"file_path": "x", "artist": None, "album": album, "title": "Song",
            "genre": None, "year": None, "track": None}  # fmt: skip


@needs_ffmpeg
def test_the_tagger_removes_a_playlist_album_tag_when_the_catalogue_has_none(m4a):
    changes = TaggerStage()._compute_changes(_row(None), _read_tags(m4a))
    assert changes["album"] == ""
    assert _write_tags(m4a, changes)
    assert "\xa9alb" not in MP4(str(m4a)).tags
    assert MP4(str(m4a)).tags["\xa9nam"] == ["Song"], "nothing else is touched"
    assert "album" not in TaggerStage()._compute_changes(_row(None), _read_tags(m4a)), "converges"


@needs_ffmpeg
def test_a_real_album_in_the_file_is_never_removed(m4a):
    audio = MP4(str(m4a))
    audio["\xa9alb"] = ["Toto IV"]
    audio.save()
    assert "album" not in TaggerStage()._compute_changes(_row(None), _read_tags(m4a))


@needs_ffmpeg
def test_a_catalogue_album_still_replaces_a_playlist_tag(m4a):
    changes = TaggerStage()._compute_changes(_row("Toto IV"), _read_tags(m4a))
    assert changes["album"] == "Toto IV"


def _probe(album):
    return {
        "format": {"tags": {"album": album, "title": "T", "artist": "A"}, "duration": "60"},
        "streams": [],
    }


@pytest.mark.parametrize("name", ["My playlist S", "my playlist", "60's British Invasion Playlist"])
def test_act_1_drops_a_playlist_name_on_arrival(name):
    from musaeus.stages.scholar import _extract_meta

    assert _extract_meta(_probe(name))["album"] is None


def test_act_1_keeps_a_real_album():
    from musaeus.stages.scholar import _extract_meta

    assert _extract_meta(_probe("Toto IV"))["album"] == "Toto IV"
    assert (
        _extract_meta(_probe("Playlist: The Very Best of ABBA"))["album"]
        == "Playlist: The Very Best of ABBA"
    )
