"""Review of the brief, B-2 (2026-10-07): AlbumArt marked a song checked before
fetching its cover, and the next run selects only unchecked songs. A refused or
timed-out lookup ("could not ask") therefore became "no cover" for good unless
someone passed --force. Confirmed by the reviewer with a probe: run 1 refused,
run 2 with the network up processed 0 files. "Could not ask" now leaves the song
unchecked, so the next run asks again; "asked, nothing there" still counts.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from musaeus import art_sources
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.albumart import AlbumArtStage

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg")


@pytest.fixture
def song(tmp_path):
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    cfg.ensure_dirs()
    ctx = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    path = cfg.alac_library / "A" / "Album" / "A - Song.m4a"
    path.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=d=1",
                    "-c:a", "alac", str(path)], check=True)  # fmt: skip
    upsert_archive(ctx.conn, {"file_path": str(path), "status": "CATALOGUED", "artist": "A",
                              "album": "Album", "title": "Song"})  # fmt: skip
    ctx.conn.commit()
    return ctx, path


def _checked(ctx, path) -> object:
    return ctx.conn.execute(
        "SELECT art_checked_at FROM archive WHERE file_path = ?", (str(path),)
    ).fetchone()[0]


def test_a_lookup_that_could_not_ask_is_asked_again(song, monkeypatch):
    ctx, path = song

    def refused(*a, **k):
        raise art_sources.ArtUnavailable("refused by network policy")

    monkeypatch.setattr(art_sources, "fetch_album_art", refused)
    monkeypatch.setattr(art_sources, "fetch_artist_picture", refused)
    AlbumArtStage().run(ctx)

    assert _checked(ctx, path) is None, "a refused lookup was recorded as checked"


def test_an_answered_lookup_with_nothing_is_still_checked(song, monkeypatch):
    ctx, path = song
    monkeypatch.setattr(art_sources, "fetch_album_art", lambda *a, **k: None)
    monkeypatch.setattr(art_sources, "fetch_artist_picture", lambda *a, **k: None)
    AlbumArtStage().run(ctx)

    assert _checked(ctx, path) is not None


# ── Review of slice A (#143), findings 10-12 ──


def test_a_cover_that_cannot_be_read_is_not_taken_for_no_cover(song, monkeypatch):
    """Finding 10: an ffprobe timeout was recorded as "no art", and a fetched
    cover was remuxed over the one the file had."""
    import musaeus.stages.albumart as albumart

    ctx, path = song

    def timed_out(*a, **k):
        raise subprocess.TimeoutExpired("ffprobe", 10)

    monkeypatch.setattr(albumart.subprocess, "run", timed_out)
    fetched: list[str] = []
    monkeypatch.setattr(art_sources, "fetch_album_art", lambda *a, **k: fetched.append("x"))
    result = AlbumArtStage().run(ctx)
    assert _checked(ctx, path) is None, "a song whose cover could not be read was stamped"
    assert not fetched, "a cover was fetched for a song that may have one"
    assert result.files_skipped == 1


def test_a_refused_upgrade_lookup_is_asked_again(song, monkeypatch):
    """Finding 11: the undersized-cover path swallowed the refusal; the row was
    already stamped checked, so its small cover stayed for good."""
    import musaeus.stages.albumart as albumart
    from musaeus.context import StageResult

    ctx, path = song
    albumart._ensure_columns(ctx.conn)
    ctx.conn.execute("UPDATE archive SET art_checked_at = '2026-10-10' WHERE file_path = ?",
                     (str(path),))  # fmt: skip

    def refused(*a, **k):
        raise art_sources.ArtUnavailable("refused by network policy")

    monkeypatch.setattr(art_sources, "fetch_album_art", refused)
    got = albumart._replace_undersized(ctx, str(path), 300, StageResult("albumart", True))
    assert got is None and _checked(ctx, path) is None


def test_sources_that_could_not_be_asked_are_not_answers(monkeypatch):
    """Finding 12: with no Last.fm key and no album, two sources asked nothing,
    and a refusal by the others came back as "no art exists"."""

    def refused(*a, **k):
        raise art_sources.ArtUnavailable("refused")

    monkeypatch.setattr(art_sources, "fetch_itunes", refused)
    monkeypatch.setattr(art_sources, "fetch_deezer", refused)
    with pytest.raises(art_sources.ArtUnavailable):
        art_sources.fetch_album_art("A", "", "")
