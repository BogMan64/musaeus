"""A cover's size is recorded when it changes (Grey, 2026-10-11): 8 masters
read 0 px while their covers were 600-1,200 px, because covers changed on
2026-10-06 never had their size recorded again."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.albumart import AlbumArtStage

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "remeasure_art.py"


def _ff(*a):
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *a],
                   check=True, capture_output=True)  # fmt: skip


def _cfg(tmp_path: Path) -> MusicConfig:
    c = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        db_path=tmp_path / "musaeus.db",
    )
    c.ensure_dirs()
    return c


def test_albumart_records_the_size_of_a_cover_it_embeds(tmp_path):
    cfg = _cfg(tmp_path)
    folder = cfg.alac_archive / "Rock" / "Band" / "Album"
    folder.mkdir(parents=True)
    song = folder / "Band - Song.m4a"
    _ff("-f", "lavfi", "-i", "sine=duration=1", "-c:a", "alac", str(song))
    _ff(
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=700x700:d=1",
        "-frames:v",
        "1",
        str(folder / "cover.jpg"),
    )
    ctx = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    upsert_archive(ctx.conn, {"file_path": str(song), "status": "CATALOGUED",
                              "artist": "Band", "album": "Album"})  # fmt: skip
    ctx.conn.commit()
    AlbumArtStage().run(ctx)
    px = ctx.conn.execute("SELECT art_px FROM archive").fetchone()[0]
    assert px == 700, f"the embedded cover's size was not recorded: {px}"


def test_the_script_measures_a_stale_size(tmp_path):
    cfg = _cfg(tmp_path)
    song = cfg.alac_archive / "Rock" / "A" / "B" / "s.m4a"
    song.parent.mkdir(parents=True)
    art = tmp_path / "a.jpg"
    _ff("-f", "lavfi", "-i", "color=c=red:s=640x640:d=1", "-frames:v", "1", str(art))
    _ff("-f", "lavfi", "-i", "sine=duration=1", "-i", str(art), "-map", "0:a", "-map", "1:v",
        "-c:a", "alac", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic", str(song))  # fmt: skip
    conn = open_db(cfg.db_path)
    upsert_archive(conn, {"file_path": str(song), "status": "CATALOGUED"})
    from musaeus.stages.albumart import _ensure_columns

    _ensure_columns(conn)  # AlbumArt's own columns, as its first run adds them
    conn.execute("UPDATE archive SET has_art = 1, art_px = 0")
    conn.commit()
    conn.close()
    env = dict(os.environ, HOME=str(tmp_path / "home"), MUSAEUS_VAULT_ROOT=str(tmp_path))
    (tmp_path / "home").mkdir()
    subprocess.run([sys.executable, str(SCRIPT), "--execute"], env=env, check=True,
                   capture_output=True, text=True)  # fmt: skip
    conn = open_db(cfg.db_path)
    assert conn.execute("SELECT art_px FROM archive").fetchone()[0] == 640
    assert (
        conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'ART_REMEASURED'").fetchone()[
            0
        ]
        == 1
    )
