"""When no album cover can be found, a picture of the artist (Grey, 2026-10-06).

Chosen by hand that day, Deezer's first exact-name match was wrong three times out of 61 (a rap
act for Bad Company, a DJ for Fergie, the album art of "Venetian Seasons" for Handel). So
Wikipedia comes first and only a page about music counts; then Deezer, the exact name with the
most fans, never its blank placeholder.
"""

from __future__ import annotations

import shutil
import subprocess
from urllib.parse import quote

import pytest
from mutagen.mp4 import MP4

from musaeus import art_sources as A
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.albumart import AlbumArtStage

JPEG = b"\xff\xd8\xff" + b"x" * 20_000


def _web(monkeypatch, pages: dict[str, dict], images: dict[str, bytes]) -> list[str]:
    asked: list[str] = []

    def get_json(url):
        asked.append(url)
        for key, value in pages.items():
            # Wikipedia titles are matched exactly, as the lookup encodes them;
            # anything else (the Deezer search) by its path.
            if url.endswith(quote(key, safe="/")) if "/summary/" in key else key in url:
                return value
        return None

    monkeypatch.setattr(A, "_get_json", get_json)
    monkeypatch.setattr(A, "_get", lambda url, **k: images.get(url))
    return asked


def _wiki(desc, img="https://w/img.jpg", kind="standard"):
    return {"type": kind, "description": desc, "originalimage": {"source": img}}


@pytest.mark.parametrize(
    ("credit", "lead"),
    [("Linda Ronstadt, James Ingram", "Linda Ronstadt"), ("Paul Simon & Dion DiMucci", "Paul Simon"),
     ("Mark Ronson feat. Bruno Mars", "Mark Ronson"), ("Four Tops", "Four Tops")],
)  # fmt: skip
def test_the_lead_artist_of_a_credit(credit, lead):
    assert A._lead_artist(credit) == lead


def test_wikipedia_first_when_the_page_is_about_music(monkeypatch):
    _web(monkeypatch, {"/summary/George_Frideric_Handel": _wiki("German-British composer")},
         {"https://w/img.jpg": JPEG})  # fmt: skip
    assert A.fetch_artist_picture("George Frideric Handel") == (JPEG, "artist picture (wikipedia)")


def test_a_page_about_someone_else_is_skipped_for_the_musician_page(monkeypatch):
    _web(monkeypatch, {"/summary/Fergie": _wiki("British royal", "https://w/royal.jpg"),
                       "/summary/Fergie_(singer)": _wiki("American singer and songwriter")},
         {"https://w/img.jpg": JPEG, "https://w/royal.jpg": JPEG})  # fmt: skip
    assert A.fetch_wikipedia_artist("Fergie") == JPEG


def test_a_disambiguation_page_is_not_a_picture(monkeypatch):
    _web(monkeypatch, {"/summary/Spirit": _wiki("Topics referred to by the same term", kind="disambiguation")},
         {"https://w/img.jpg": JPEG})  # fmt: skip
    assert A.fetch_wikipedia_artist("Spirit") is None


def test_deezer_takes_the_exact_name_with_the_most_fans(monkeypatch):
    rap = {"name": "Bad Company", "nb_fan": 12, "picture_xl": "https://d/rap.jpg"}
    band = {"name": "Bad Company", "nb_fan": 450_000, "picture_xl": "https://d/band.jpg"}
    other = {"name": "Bad Company UK", "nb_fan": 9_000_000, "picture_xl": "https://d/other.jpg"}
    _web(monkeypatch, {"search/artist": {"data": [rap, other, band]}},
         {"https://d/band.jpg": JPEG, "https://d/rap.jpg": b"\xff\xd8\xff" + b"r" * 20_000})  # fmt: skip
    assert A.fetch_deezer_artist("Bad Company") == JPEG


def test_deezers_blank_placeholder_is_never_used(monkeypatch):
    blank = {
        "name": "Adam Clayton",
        "nb_fan": 5,
        "picture_xl": "https://e-cdns-images.dzcdn.net/images/artist//1000x1000.jpg",
    }
    _web(monkeypatch, {"search/artist": {"data": [blank]}}, {blank["picture_xl"]: JPEG})
    assert A.fetch_deezer_artist("Adam Clayton") is None


def test_deezer_when_wikipedia_has_nothing(monkeypatch):
    band = {"name": "Go West", "nb_fan": 80_000, "picture_xl": "https://d/gw.jpg"}
    _web(monkeypatch, {"search/artist": {"data": [band]}}, {"https://d/gw.jpg": JPEG})
    assert A.fetch_artist_picture("Go West") == (JPEG, "artist picture (deezer)")


def test_nothing_askable_is_unavailable_not_none(monkeypatch):
    def refuse(*a, **k):
        raise A.ArtUnavailable("offline")

    monkeypatch.setattr(A, "_get_json", refuse)
    with pytest.raises(A.ArtUnavailable):
        A.fetch_artist_picture("Anyone")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg")
def test_the_stage_uses_an_artist_picture_when_no_album_cover_exists(tmp_path, monkeypatch):
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q", runs_root=tmp_path / "RUNS", meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library", db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    cfg.meta_dir.mkdir(parents=True)
    ctx = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    folder = tmp_path / "INBOX" / "Album"
    folder.mkdir(parents=True)
    song = folder / "a.m4a"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1", "-c:a", "alac", str(song)],
                   check=True, capture_output=True)  # fmt: skip
    photo = tmp_path / "p.jpg"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=green:s=600x600:d=1", "-frames:v", "1", str(photo)],
                   check=True, capture_output=True)  # fmt: skip
    upsert_archive(ctx.conn, {"file_path": str(song), "status": "CATALOGUED", "artist": "Go West",
                              "album": "", "title": "a"})  # fmt: skip
    ctx.conn.commit()
    monkeypatch.setattr(A, "fetch_album_art", lambda *a, **k: None)
    monkeypatch.setattr(
        A, "fetch_artist_picture", lambda artist: (photo.read_bytes(), "artist picture (test)")
    )
    AlbumArtStage().execute(ctx)
    assert "covr" in MP4(song).tags
    assert not (folder / "cover.jpg").exists()  # an artist picture is never a folder's cover
