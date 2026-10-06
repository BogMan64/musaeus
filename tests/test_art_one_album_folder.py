"""A folder's cover.jpg belongs to one album; a picture on many artists is reported (2026-10-06).

223 songs wore other albums' covers -- one picture on 159 songs by 108 artists. 41 of them were
put there by this stage: the USB1 inbox is one flat folder of every artist's songs, and a cover
fetched for one song was saved as the folder's cover.jpg, "so the next file in the same album
reuses it", and then went onto every song after it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.mp4 import MP4, MP4Cover

from musaeus import art_sources, doctor
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages import albumart
from musaeus.stages.albumart import AlbumArtStage, _one_album_folder

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg")


def _alac(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1", "-c:a", "alac", str(path)],
                   check=True, capture_output=True)  # fmt: skip
    return path


def _jpeg(path: Path, colour: str = "red") -> bytes:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"color=c={colour}:s=600x600:d=1", "-frames:v", "1", str(path)],
                   check=True, capture_output=True)  # fmt: skip
    return path.read_bytes()


@pytest.fixture
def ctx(tmp_path):
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q", runs_root=tmp_path / "RUNS", meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library", db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    cfg.meta_dir.mkdir(parents=True)
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _row(ctx, path, artist, album):
    upsert_archive(ctx.conn, {"file_path": str(path), "status": "CATALOGUED", "artist": artist,
                              "album": album, "title": path.stem})  # fmt: skip
    ctx.conn.commit()


def test_a_folder_of_one_album_is_one_album(ctx, tmp_path):
    folder = tmp_path / "INBOX" / "Hearts"
    _row(ctx, folder / "a.m4a", "America", "Hearts")
    _row(ctx, folder / "b.m4a", "America", "Hearts")
    assert _one_album_folder(ctx, folder)


def test_a_flat_folder_of_many_artists_is_not(ctx, tmp_path):
    folder = tmp_path / "INBOX" / "USB1_Curated_RAW"
    _row(ctx, folder / "a.m4a", "America", "Hearts")
    _row(ctx, folder / "b.m4a", "Glenn Miller", "Kings of Swing")
    _row(ctx, folder / "sub" / "c.m4a", "Glenn Miller", "Other")  # a subfolder is its own folder
    assert not _one_album_folder(ctx, folder)


@needs_ffmpeg
def test_a_mixed_folders_cover_jpg_is_never_embedded(ctx, tmp_path, monkeypatch):
    folder = tmp_path / "INBOX" / "flat"
    a, b = _alac(folder / "a.m4a"), _alac(folder / "b.m4a")
    _row(ctx, a, "America", "Hearts")
    _row(ctx, b, "Glenn Miller", "Kings of Swing")
    _jpeg(folder / "cover.jpg")  # some other album's picture
    monkeypatch.setattr(art_sources, "fetch_album_art", lambda *a, **k: None)
    AlbumArtStage().execute(ctx)
    assert not MP4(a).tags or "covr" not in MP4(a).tags
    assert not MP4(b).tags or "covr" not in MP4(b).tags


@needs_ffmpeg
def test_a_cover_fetched_in_a_mixed_folder_goes_on_its_own_song_only(ctx, tmp_path, monkeypatch):
    folder = tmp_path / "INBOX" / "flat"
    a, b = _alac(folder / "a.m4a"), _alac(folder / "b.m4a")
    _row(ctx, a, "America", "Hearts")
    _row(ctx, b, "Glenn Miller", "Kings of Swing")
    red, blue = _jpeg(tmp_path / "red.jpg", "red"), _jpeg(tmp_path / "blue.jpg", "blue")
    covers = {"America": red, "Glenn Miller": blue}
    monkeypatch.setattr(
        art_sources, "fetch_album_art", lambda artist, album, *a, **k: (covers[artist], "test")
    )
    AlbumArtStage().execute(ctx)
    assert bytes(MP4(a).tags["covr"][0]) != bytes(MP4(b).tags["covr"][0])
    assert not (folder / "cover.jpg").exists()
    assert not list(folder.glob(f"*{albumart._PRIVATE_COVER_SUFFIX}"))  # removed once embedded


def _master_with_cover(root: Path, artist: str, blob: bytes, song: str = "Song") -> None:
    p = _alac(root / "Rock" / artist / "Album" / f"{artist} - {song}.m4a")
    m = MP4(p)
    if m.tags is None:
        m.add_tags()
    m.tags["covr"] = [MP4Cover(blob, imageformat=MP4Cover.FORMAT_JPEG)]
    m.save()


class _Rep:
    def __init__(self):
        self.items = []

    def add(self, *a):
        self.items.append(a)


@needs_ffmpeg
def test_doctor_reports_one_picture_on_many_artists(tmp_path):
    root = tmp_path / "ALAC-Archival"
    blob = _jpeg(tmp_path / "x.jpg")
    for artist in ("America", "Glenn Miller", "The Beach Boys", "Barlow"):
        _master_with_cover(root, artist, blob)
    rep = _Rep()
    doctor._one_picture_many_artists(type("C", (), {"alac_archive": root})(), rep)
    assert rep.items[0][0] == "warn" and "4 songs by 4 artists" in rep.items[0][2]


@needs_ffmpeg
def test_doctor_is_quiet_for_one_artists_album(tmp_path):
    # Delerium's "Remixed": four duet credits, one artist folder, one album cover
    root = tmp_path / "ALAC-Archival"
    blob = _jpeg(tmp_path / "x.jpg")
    for n in range(4):
        _master_with_cover(root, "Delerium", blob, song=f"Song {n}")
    rep = _Rep()
    doctor._one_picture_many_artists(type("C", (), {"alac_archive": root})(), rep)
    assert rep.items[0][0] == "ok"
