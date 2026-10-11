"""Review of slice A (#143), the last low findings:
- BPM's preview counted `bpm IS NULL`; its run selects on bpm_analyzed_at.
- Tagger's FLAC/MP3 writers dropped sort_artist and reported success, so
  those masters never converged.
- AlbumArt's upgrade overwrote a one-album folder's cover.jpg -- possibly a
  cover Grey chose by hand, and the one the folder's other songs take.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages import albumart, tagger
from musaeus.stages.bpm import BPMStage

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="requires ffmpeg"
)


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        db_path=tmp_path / "musaeus.db",
    )
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                   check=True, capture_output=True)  # fmt: skip


def test_bpms_preview_counts_what_its_run_selects(ctx):
    # A bpm read from the file's tag, never analysed: the run selects it.
    upsert_archive(ctx.conn, {"file_path": "/m/a.m4a", "status": "CATALOGUED"})
    ctx.conn.execute("UPDATE archive SET bpm = 120.0, bpm_analyzed_at = NULL")
    ctx.conn.commit()
    n, _ = BPMStage.plan_candidates(ctx.conn, ctx.config)
    assert n == len(BPMStage()._get_pending(ctx, force=False)) == 1


@needs_ffmpeg
@pytest.mark.parametrize("ext", [".flac", ".mp3"])
def test_tagger_writes_and_reads_the_sort_artist(tmp_path, ext):
    f = tmp_path / f"song{ext}"
    _ff("-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(f))
    assert tagger._write_tags(f, {"artist": "The Beatles", "sort_artist": "Beatles, The"})
    assert tagger._read_tags(f).get("sort_artist") == "Beatles, The"


@needs_ffmpeg
def test_an_art_upgrade_leaves_the_folders_cover_alone(ctx, tmp_path, monkeypatch):
    folder = ctx.config.alac_archive / "Rock" / "Band" / "Album"
    folder.mkdir(parents=True)
    small, big = tmp_path / "small.jpg", tmp_path / "big.jpg"
    _ff("-f", "lavfi", "-i", "color=c=blue:s=300x300:d=1", "-frames:v", "1", str(small))
    _ff("-f", "lavfi", "-i", "color=c=red:s=1000x1000:d=1", "-frames:v", "1", str(big))
    song = folder / "Band - Song.m4a"
    _ff("-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-i", str(small),
        "-map", "0:a", "-map", "1:v", "-c:a", "alac", "-c:v", "mjpeg",
        "-disposition:v:0", "attached_pic", str(song))  # fmt: skip
    chosen = folder / "cover.jpg"
    chosen.write_bytes(small.read_bytes())  # Grey's own pick for the album
    upsert_archive(
        ctx.conn,
        {"file_path": str(song), "status": "CATALOGUED", "artist": "Band", "album": "Album"},
    )
    ctx.conn.commit()
    monkeypatch.setattr(albumart, "_replace_undersized", lambda *a, **k: big.read_bytes())
    albumart.AlbumArtStage().run(ctx)
    assert chosen.read_bytes() == small.read_bytes(), "the folder's cover.jpg was replaced"
