"""Codex audit 2026-10-16, F1: the "safe" save followed links.

change_beside copied the song onto <name><suffix> with shutil.copy2, which
writes THROUGH a link already at that name: a planted `song.m4a.artmp` link
to another recording had that recording overwritten, and the rename then put
the link in the song's place. Cover sidecars were written with write_bytes,
through a planted `<song>.musaeus_cover.jpg` or dangling `cover.jpg` link.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from musaeus.safe_save import change_beside


def _pair(tmp_path: Path) -> tuple[Path, Path]:
    song = tmp_path / "inbox" / "song.m4a"
    other = tmp_path / "masters" / "other.m4a"
    song.parent.mkdir()
    other.parent.mkdir()
    song.write_bytes(b"SONG")
    other.write_bytes(b"ANOTHER RECORDING")
    return song, other


@pytest.mark.parametrize("how", ["symlink", "hardlink", "dangling", "stale file"])
def test_whatever_is_at_the_copys_name_is_never_written_through(tmp_path, how):
    song, other = _pair(tmp_path)
    tmp = song.with_name(song.name + ".artmp")
    if how == "symlink":
        tmp.symlink_to(other)
    elif how == "hardlink":
        os.link(other, tmp)
    elif how == "dangling":
        tmp.symlink_to(tmp_path / "masters" / "not-there.m4a")
    else:
        tmp.write_bytes(b"LEFT BY A KILLED RUN")

    change_beside(song, lambda p: p.write_bytes(p.read_bytes() + b"+TAGS"), suffix=".artmp")

    assert other.read_bytes() == b"ANOTHER RECORDING", "another recording was overwritten"
    assert not (tmp_path / "masters" / "not-there.m4a").exists()
    assert not song.is_symlink() and song.read_bytes() == b"SONG+TAGS"
    assert not tmp.exists() and not tmp.is_symlink()


def test_a_refused_change_leaves_both_files_as_they_were(tmp_path):
    song, other = _pair(tmp_path)
    tmp = song.with_name(song.name + ".tagging")
    tmp.symlink_to(other)

    def refuse(p: Path) -> None:
        raise ValueError("no")

    with pytest.raises(ValueError):
        change_beside(song, lambda p: None, suffix=".tagging", check=refuse)

    assert other.read_bytes() == b"ANOTHER RECORDING" and song.read_bytes() == b"SONG"
    assert not song.is_symlink()


@pytest.mark.parametrize("dangling", [False, True])
def test_a_cover_is_never_written_through_a_link(tmp_path, dangling):
    song, other = _pair(tmp_path)
    from musaeus.safe_save import write_new

    cover = song.parent / "cover.jpg"
    target = tmp_path / "masters" / ("nowhere.jpg" if dangling else "other.m4a")
    cover.symlink_to(target)

    write_new(cover, b"\xff\xd8\xff IMAGE")

    assert other.read_bytes() == b"ANOTHER RECORDING"
    assert not (tmp_path / "masters" / "nowhere.jpg").exists()
    assert not cover.is_symlink() and cover.read_bytes() == b"\xff\xd8\xff IMAGE"
    assert {p.name for p in song.parent.iterdir()} == {"cover.jpg", "song.m4a"}, "temp left"


def test_the_album_art_stage_never_writes_a_cover_through_a_link(tmp_path, monkeypatch):
    # Review of #205: the stage's own two sidecar writes, end to end. A dangling
    # cover.jpg link sent a fetched cover to a new file outside the library.
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    from musaeus import art_sources
    from musaeus.config import MusicConfig
    from musaeus.context import RunContext
    from musaeus.db import open_db, upsert_archive
    from musaeus.stages.albumart import AlbumArtStage

    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    cfg.ensure_dirs()
    ctx = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    song = cfg.alac_library / "A" / "Album" / "A - Song.m4a"
    song.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=d=1",
                    "-c:a", "alac", str(song)], check=True)  # fmt: skip
    cover = tmp_path / "cover_src.jpg"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=800x800:rate=1", "-frames:v", "1", str(cover)], check=True)  # fmt: skip
    upsert_archive(ctx.conn, {"file_path": str(song), "status": "CATALOGUED", "artist": "A",
                              "album": "Album", "title": "Song"})  # fmt: skip
    ctx.conn.commit()
    outside = tmp_path / "elsewhere" / "planted.jpg"
    outside.parent.mkdir()
    (song.parent / "cover.jpg").symlink_to(outside)  # dangling
    monkeypatch.setattr(art_sources, "fetch_album_art", lambda *a, **k: (cover.read_bytes(), "x"))
    monkeypatch.setattr(art_sources, "fetch_artist_picture", lambda *a, **k: None)

    AlbumArtStage().run(ctx)

    assert not outside.exists(), "the cover was written outside the library through a link"
    # And the stage really did its work: the link was replaced by the fetched
    # image, not skipped (Codex review of #205).
    sidecar = song.parent / "cover.jpg"
    assert not sidecar.is_symlink() and sidecar.read_bytes() == cover.read_bytes()


def test_copy_to_new_never_writes_through_and_leaks_nothing(tmp_path):
    # Shared by change_beside, Finalize's .finalize_tmp and the bit-rot repair's
    # .restoring (review of #205: the same pattern lived there too).
    from musaeus.safe_save import copy_to_new

    song, other = _pair(tmp_path)
    dst = song.with_name("song.m4a.finalize_tmp")
    dst.symlink_to(other)
    copy_to_new(song, dst)
    assert other.read_bytes() == b"ANOTHER RECORDING"
    assert not dst.is_symlink() and dst.read_bytes() == b"SONG"

    before = len(os.listdir("/proc/self/fd"))
    for _ in range(5):
        with pytest.raises(OSError):
            copy_to_new(tmp_path / "missing.m4a", tmp_path / "x.tmp")
    assert len(os.listdir("/proc/self/fd")) == before, "a handle leaked"
    assert not (tmp_path / "x.tmp").exists()
