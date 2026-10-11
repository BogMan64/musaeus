"""`musaeus doctor`: song sort names that are not the title (Grey, 2026-10-11)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.mp4 import MP4

from musaeus import doctor
from musaeus.config import MusicConfig
from musaeus.db import open_db, upsert_archive

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg")


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


def _master(cfg, name, title, sort):
    f = cfg.alac_archive / "Rock" / "A" / "B" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=duration=1", "-c:a", "alac", str(f)],
                   check=True, capture_output=True)  # fmt: skip
    m = MP4(str(f))
    m["\xa9nam"] = [title]
    if sort is not None:
        m["sonm"] = [sort]
    m.save()
    conn = open_db(cfg.db_path)
    upsert_archive(conn, {"file_path": str(f), "status": "CATALOGUED"})
    conn.commit()
    conn.close()


def _finding(cfg):
    rep = doctor.Report()
    doctor._sort_titles_that_disagree(cfg, rep)
    (f,) = [x for x in rep.findings if x.check == "song sort names"]
    return f


def test_a_stale_sort_name_is_reported(tmp_path):
    cfg = _cfg(tmp_path)
    _master(cfg, "a.m4a", "Midnight Flyer", "Midnight Flyer (2013 Remaster)")
    f = _finding(cfg)
    assert f.level == "warn" and f.count == 1 and "2013 Remaster" in f.detail


def test_a_dropped_article_or_no_sort_name_is_fine(tmp_path):
    cfg = _cfg(tmp_path)
    _master(cfg, "a.m4a", "The Weight", "Weight")
    _master(cfg, "b.m4a", "Brown Sugar", None)
    _master(cfg, "c.m4a", "Angie", "angie")
    f = _finding(cfg)
    assert f.level == "ok" and "1 drop only" in f.detail
