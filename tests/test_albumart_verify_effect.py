"""AlbumArt must check the FILE, not its own bookkeeping.

This is the stage that made the case for the whole mechanism. On
2026-08-31 it reported

    albumart: OK ✓verified | processed=10554 changed=10549

while every single embed failed: `_embed_art` named its temp file
`track.m4a.artmp`, ffmpeg could not infer a muxer from `.artmp`, and the
encode died every time. ART_EMBEDDED had been zero for the project's
entire history and nothing noticed, because the stage's claim was checked
against nothing at all.

The check therefore reads `has_art` back from ffprobe rather than from the
database column this stage just wrote. A stage that confirms its own
bookkeeping proves only that it can write to SQLite -- which was never in
doubt. It has to ask the file.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.db import open_db
from musaeus.stages.albumart import AlbumArtStage

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="requires ffmpeg and ffprobe",
)


class _Ctx:
    def __init__(self, conn):
        self.conn = conn


@pytest.fixture
def conn(tmp_path: Path):
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )
    cfg.ensure_dirs()
    c = open_db(cfg.db_path)
    for col in ("has_art INTEGER", "art_checked_at TEXT", "art_px INTEGER"):
        name = col.split()[0]
        cols = {r[1] for r in c.execute("PRAGMA table_info(archive)")}
        if name not in cols:
            c.execute(f"ALTER TABLE archive ADD COLUMN {col}")
    c.commit()
    yield c
    c.close()


def _alac(path: Path, with_art: bool) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "alac", str(path)], check=True, capture_output=True)
    if not with_art:
        return
    art = path.with_suffix(".jpg")
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=red:s=600x600:d=1", "-frames:v", "1",
         str(art)], check=True, capture_output=True)
    out = path.with_name("withart.m4a")
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-i", str(art), "-map", "0:a", "-map", "1:v", "-c:a", "copy",
         "-c:v", "mjpeg", "-disposition:v:0", "attached_pic", str(out)],
        check=True, capture_output=True)
    out.replace(path)


def _row(conn, path: Path, has_art: int) -> None:
    conn.execute(
        "INSERT INTO archive (file_path, status, has_art, art_checked_at) "
        "VALUES (?,?,?,datetime('now'))", (str(path), "CATALOGUED", has_art))
    conn.commit()


@needs_ffmpeg
def test_a_file_that_really_has_art_verifies(conn, tmp_path: Path) -> None:
    f = tmp_path / "good.m4a"
    _alac(f, with_art=True)
    _row(conn, f, 1)
    assert AlbumArtStage().verify_effect(_Ctx(conn), None) == []


@needs_ffmpeg
def test_the_2026_08_31_failure_is_now_caught(conn, tmp_path: Path) -> None:
    """The exact shape of the bug: the row says has_art=1, the file has
    none, and the stage previously printed ✓verified over it."""
    f = tmp_path / "bad.m4a"
    _alac(f, with_art=False)
    _row(conn, f, 1)
    problems = AlbumArtStage().verify_effect(_Ctx(conn), None)
    assert problems, "a row claiming art over an artless file must be reported"
    assert "ffprobe finds none" in problems[0]


@needs_ffmpeg
def test_it_asks_the_file_not_the_column(conn, tmp_path: Path) -> None:
    """Confirming its own bookkeeping would prove only that the stage can
    write to SQLite. Both files below have has_art=1; only one has art."""
    good, bad = tmp_path / "g.m4a", tmp_path / "b.m4a"
    _alac(good, with_art=True)
    _alac(bad, with_art=False)
    _row(conn, good, 1)
    _row(conn, bad, 1)
    problems = AlbumArtStage().verify_effect(_Ctx(conn), None)
    assert problems and "1 of 2" in problems[0]


def test_no_rows_means_no_complaint(conn) -> None:
    """Nothing to verify is not a failure."""
    assert AlbumArtStage().verify_effect(_Ctx(conn), None) == []


def test_a_missing_file_is_not_counted_as_artless(conn, tmp_path: Path) -> None:
    """A file gone from disk is a different fault, and belongs to whichever
    stage moves files -- not to a false 'has no art' report here."""
    ghost = tmp_path / "gone.m4a"
    _row(conn, ghost, 1)
    assert AlbumArtStage().verify_effect(_Ctx(conn), None) == []
