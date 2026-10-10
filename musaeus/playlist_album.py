"""An album name that is really a playlist's name -- not an album.

Grey, 2026-10-03: "all references to playlist, My Playlist, or my playlist with
any letter [should] be removed ... also for tracks inside ALAC-Archival".

The USB1 batch came out of per-letter playlist folders, so 1,841 songs carried
"My playlist S", "My playlist J" ... as their ALBUM -- and were filed into
folders of that name. It is a source-folder artefact, not metadata.

What counts: "My playlist ..." in any case or letter, and a name ENDING in the
word "Playlist" ("60's British Invasion Playlist"). What does not: a real
album with the word elsewhere -- Sony's "Playlist: The Very Best of ..."
series -- because wiping those would delete true albums.
"""

from __future__ import annotations

import re

_PLAYLIST_ALBUM = re.compile(r"^\s*my\s+playlist\b|\bplaylist\s*$", re.IGNORECASE)


def is_playlist_album(album: str | None) -> bool:
    """True when *album* is a playlist's name standing in for an album."""
    return bool(album and _PLAYLIST_ALBUM.search(album))
