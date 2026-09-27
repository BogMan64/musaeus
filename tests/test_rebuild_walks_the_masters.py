"""rebuild-from-disk rebuilds the catalogue from the MASTERS.

It walked ALAC_Library, the tier finalize filed into before 2026-09-25.
Masters now live in ALAC-Archival and ALAC_Library holds the Lossless
edition -- so a rebuild after the edition is built would have made every
-18 LUFS copy a catalogue row and lost every master: the retired bake's
drift, all at once.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.db import open_db
from musaeus.rebuild_from_disk import scan_and_rebuild

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")


def _audio(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=2", "-c:a", "alac", str(path)],
        check=True,
    )  # fmt: skip


def test_the_rebuild_reads_the_masters_not_the_edition(tmp_path):
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
    cfg.ensure_dirs()
    rel = Path("Rock") / "Stones" / "Sticky Fingers" / "The Rolling Stones - Brown Sugar.m4a"
    _audio(cfg.alac_archive / rel)
    _audio(cfg.alac_library / rel)  # its -18 LUFS edition copy
    conn = open_db(cfg.db_path)
    scan_and_rebuild(conn, cfg, compute_hashes=False)
    conn.row_factory = sqlite3.Row
    paths = [r["file_path"] for r in conn.execute("SELECT file_path FROM archive_rebuilt")]
    assert paths == [str(cfg.alac_archive / rel)], paths
