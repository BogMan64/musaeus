"""Review of slice A (#143): AlbumFill chose its rows with
`album LIKE 'My playlist%'`, which disagreed with playlist_album's rule -- a
name ending in "Playlist" ("60's British Invasion Playlist") was a playlist
everywhere else in MUSAEUS and a real album here, never filled."""

from __future__ import annotations

from musaeus.db import open_db, upsert_archive
from musaeus.playlist_album import is_playlist_album
from musaeus.stages.album_fill import AlbumFillStage

ALBUMS = [
    "My playlist S",
    "  my playlist j",
    "60's British Invasion Playlist",
    "Playlist: The Very Best of Journey",  # a real album (Sony's series)
    "Abbey Road",
]


def test_album_fill_counts_what_the_shared_rule_calls_a_playlist(tmp_path):
    conn = open_db(tmp_path / "m.db")
    for i, album in enumerate(ALBUMS):
        upsert_archive(conn, {"file_path": f"/m/{i}.m4a", "status": "CATALOGUED", "album": album})
    # Checked already: only the playlist count is under test, not arriving albums.
    conn.execute("UPDATE archive SET album_verified_at='checked'")
    conn.commit()
    n, _ = AlbumFillStage.plan_candidates(conn, None)
    assert n == sum(is_playlist_album(a) for a in ALBUMS) == 3
