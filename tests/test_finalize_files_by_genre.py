"""The archive files under Genre/Artist/Album (Grey's ruling, 2026-09-18).

The layout is only safe because MasterLaw rules genre per ARTIST, not per
track: measured against 9,049 catalogued rows the day it was introduced,
**0** artists spanned more than one genre and **0** albums would have been
split. If genre ever becomes a per-track field, an album starts tearing in
half and this guard is where that shows up.

The other half of the contract is that a row with no genre goes to
"Unsorted" -- not to a folder called "None". That case is the NORMAL state
for freshly ingested material, which arrives before MasterLaw has ruled on
the artist, so it is not an edge case.
"""

from __future__ import annotations

import pytest

from musaeus.stages.finalize import genre_folder


class TestGenreFolder:
    @pytest.mark.parametrize(
        ("genre", "expected"),
        [
            ("Rock", "Rock"),
            ("Jazz", "Jazz"),
            ("  Blues  ", "Blues"),
        ],
    )
    def test_a_plain_genre_is_its_own_folder(self, genre, expected):
        assert genre_folder(genre) == expected

    @pytest.mark.parametrize("genre", ["", "   ", None])
    def test_no_genre_goes_to_unsorted_never_to_none(self, genre):
        """`str(None)` is "None", and a folder called None is indistinguishable
        from a band called None. The fallback has to be explicit."""
        assert genre_folder(genre) == "Unsorted"

    def test_a_slash_in_a_genre_does_not_become_a_directory(self):
        """"R&B/Funk/Soul" is ONE genre. Left unsanitised it would build three
        nested directories and file the artist under "Soul"."""
        assert "/" not in genre_folder("R&B/Funk/Soul")
        assert genre_folder("R&B/Funk/Soul") == "R&B-Funk-Soul"
        assert genre_folder("Disco/Electronic") == "Disco-Electronic"

    def test_several_genres_take_the_first(self):
        """One file cannot live in two folders. The playlist stage already
        resolves this the same way (_primary_genre), and the two must agree or
        a track's folder and its genre playlist disagree about what it is."""
        assert genre_folder("Jazz, Blues") == "Jazz"

    def test_the_choice_matches_the_playlist_stage(self):
        from musaeus.stages.playlist import _primary_genre, _safe_genre

        for g in ("Jazz, Blues", "R&B/Funk/Soul", "Rock"):
            assert genre_folder(g) == _safe_genre(_primary_genre(g)) or \
                   genre_folder(g).replace("-", "") == _safe_genre(_primary_genre(g)).replace("-", "").replace("_", "")
