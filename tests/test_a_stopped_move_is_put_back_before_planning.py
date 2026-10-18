"""Codex audit 2026-10-16, F11: a build's move phase steps each moving copy aside
to <name>.<hash12>.edition_tmp before moving it. A build killed in between left
finished copies there, and the next build put them back -- but inside execute,
after its plan was made. The plan saw the copy as missing and baked it again at
its new place; then the old copy was put back as well. Two finished copies, the
ledger knowing only the new one, and the next plan listed the old one as an
unknown file.

Now the command puts them back before planning, under the build lock, and the
interrupted move simply finishes. The whole lifecycle, through the command.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from musaeus import edition_build as eb
from musaeus.config import MusicConfig
from musaeus.db import open_db, upsert_archive
from musaeus.edition_ledger import copies, ledger_path, open_ledger

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("fdkaac")), reason="needs ffmpeg and fdkaac"
)

H = "abcdef0123456789" * 4
OLD = "Rock/A/Album/A - Song.m4a"
NEW = "Rock/A/Other Album/A - Song.m4a"


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
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


def _cli(cfg, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "MUSAEUS_VAULT_ROOT": str(cfg.vault_root),
           "MUSAEUS_DB_PATH": str(cfg.db_path),
           "MUSAEUS_NO_SLEEP_INHIBIT": "1", "MUSAEUS_NO_IDLE_THROTTLE": "1"}  # fmt: skip
    return subprocess.run(
        [sys.executable, "-m", "musaeus.cli", "edition-build", "car", *args],
        capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
        cwd=Path(__file__).resolve().parents[1], timeout=180,
    )  # fmt: skip


def _a_build_killed_mid_move(cfg) -> tuple[Path, Path]:
    """Build, refile the master, then leave the state a killed move leaves:
    the finished copy stepped aside, nothing yet at its new place."""
    master = cfg.alac_archive / OLD
    master.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=6", "-c:a", "alac", "-sample_fmt", "s16p",
         str(master)],
        check=True, stdin=subprocess.DEVNULL,
    )  # fmt: skip
    conn = open_db(cfg.db_path)
    upsert_archive(conn, {"file_path": str(master), "status": "CATALOGUED", "audio_hash": H,
                          "codec": "alac", "size_bytes": master.stat().st_size})  # fmt: skip
    conn.commit()
    r = _cli(cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    old_copy = cfg.car_library / "A" / "Album" / "A - Song.m4a"
    assert old_copy.is_file(), r.stdout

    moved = cfg.alac_archive / NEW
    moved.parent.mkdir(parents=True)
    master.rename(moved)
    conn.execute("UPDATE archive SET file_path = ? WHERE file_path = ?", (str(moved), str(master)))
    conn.commit()
    conn.close()
    aside = old_copy.with_name(f"{old_copy.name}.{H[:12]}{eb.TMP_SUFFIX}")
    old_copy.rename(aside)
    return old_copy, aside


def _finished_copies(cfg) -> list[Path]:
    return sorted(p for p in cfg.car_library.rglob("*") if p.is_file())


def test_the_interrupted_move_finishes_and_nothing_is_baked_again(cfg):
    _, aside = _a_build_killed_mid_move(cfg)

    r = _cli(cfg)

    assert r.returncode == 0, r.stdout + r.stderr
    assert "Baked 0" in r.stdout and "moved 1" in r.stdout, r.stdout
    new_copy = cfg.car_library / "A" / "Other Album" / "A - Song.m4a"
    assert _finished_copies(cfg) == [new_copy], "two finished copies, or none"
    assert not aside.exists()
    ledger = open_ledger(ledger_path(cfg))
    try:
        assert copies(ledger, "car")[H].output_path == str(new_copy)
        conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        plan = eb.make_plan(conn, ledger, cfg.alac_archive, cfg.car_library, kind=eb.CAR_KIND)
        conn.close()
    finally:
        ledger.close()
    assert plan.up_to_date == 1 and not (plan.bake or plan.move or plan.unrecorded), (
        "the next build is not settled"
    )


def test_a_preview_says_so_and_changes_nothing(cfg):
    _, aside = _a_build_killed_mid_move(cfg)

    r = _cli(cfg, "--dry-run")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "will be put back first" in r.stdout
    assert aside.is_file(), "a preview put a copy back"
