"""
Tests for Phase 2A -- scripts/alac_library/build_alac_library.py.

Confirms the ALAC_Archive -> ALAC-Library LUFS bake actually works
end-to-end: a real loud synthetic ALAC source, baked to ~-18 LUFS, with
archive.file_path moved to the new ALAC-Library location and
lufs_baked_at/lufs_baked_target recorded -- not just "the script exited 0".

Entirely scoped to pytest's tmp_path: a disposable MusicConfig/DB, a
scratch ALAC_Archive/ALAC-Library pair. Never touches the real vault.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.db import open_db, upsert_archive

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not available",
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "alac_library" / "build_alac_library.py"


def _script_argv(script_path: Path) -> list[str]:
    """Normally just [python3, script.py]. When MUSAEUS_COVERAGE_SUBPROCESS
    is set, wrap in `coverage run --parallel-mode` so this subprocess's
    execution shows up in the project's coverage numbers -- see
    pyproject.toml's [tool.coverage.run]. Opt-in only."""
    if os.environ.get("MUSAEUS_COVERAGE_SUBPROCESS"):
        # No --rcfile: pyproject.toml's `source` is a relative path,
        # resolved against this subprocess's cwd, not repo root -- fragile
        # if cwd= is ever added to this call. --parallel-mode + the
        # absolute COVERAGE_FILE (below) are all this child needs;
        # source-filtering happens later at report time.
        return [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--parallel-mode",
            str(script_path),
        ]
    return [sys.executable, str(script_path)]


def _gen_loud_alac(path: Path, duration: int = 3) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration}",
        "-af",
        "volume=12dB",
        "-c:a",
        "alac",
        str(path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def _measure_lufs(path: Path) -> float:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-af",
        "loudnorm=print_format=json",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    stderr = result.stderr or ""
    start, end = stderr.rfind("{"), stderr.rfind("}")
    return float(json.loads(stderr[start : end + 1])["input_i"])


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
    return MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )


def _run_script(
    cfg: MusicConfig, archive_dir: Path, extra_args: list[str]
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["MUSAEUS_VAULT_ROOT"] = str(cfg.vault_root)
    env["MUSAEUS_DB_PATH"] = str(cfg.db_path)
    env["MUSAEUS_ALAC_LIBRARY"] = str(cfg.alac_library)
    if os.environ.get("MUSAEUS_COVERAGE_SUBPROCESS"):
        # Absolute path so parallel-mode data files land in one place
        # regardless of this subprocess's cwd.
        env["COVERAGE_FILE"] = str(_REPO_ROOT / ".coverage")
        # conftest.py deliberately redirects HOME to a fake session directory
        # for the whole test run (so config.py's _load_env() can't leak real
        # ~/.config/musaeus/credentials.env into tests) -- correct, not
        # touched here. But it also breaks `python3 -m coverage`'s own
        # ~/.local-based module resolution in a subprocess that inherits
        # this env. Point PYTHONPATH at coverage's actual install location
        # explicitly instead of restoring HOME.
        import coverage as _coverage

        site_packages = str(Path(_coverage.__file__).resolve().parent.parent)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{site_packages}:{existing}" if existing else site_packages
    return subprocess.run(
        [*_script_argv(_SCRIPT), "--archive-dir", str(archive_dir), *extra_args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestPhase2ABake:
    def test_dry_run_makes_no_changes(self, cfg: MusicConfig) -> None:
        archive_dir = cfg.vault_root / "ALAC_Archive"
        archive_dir.mkdir(parents=True)
        src = archive_dir / "Artist" / "Album" / "track.m4a"
        src.parent.mkdir(parents=True)
        _gen_loud_alac(src)

        conn = open_db(cfg.db_path)
        upsert_archive(
            conn,
            {
                "file_path": str(src),
                "filename": src.name,
                "ext": ".m4a",
                "status": "CATALOGUED",
                "codec": "alac",
                "artist": "Artist",
                "title": "Track",
            },
        )
        conn.commit()
        conn.close()

        result = _run_script(cfg, archive_dir, [])
        assert result.returncode == 0, result.stderr
        assert "WOULD BAKE" in result.stdout
        assert not cfg.alac_library.exists() or not any(cfg.alac_library.rglob("*.m4a"))

        conn = open_db(cfg.db_path)
        row = conn.execute("SELECT file_path, lufs_baked_at FROM archive").fetchone()
        conn.close()
        assert row["file_path"] == str(src)
        assert row["lufs_baked_at"] is None

    def test_execute_bakes_and_updates_db(self, cfg: MusicConfig) -> None:
        archive_dir = cfg.vault_root / "ALAC_Archive"
        archive_dir.mkdir(parents=True)
        src = archive_dir / "Artist" / "Album" / "track.m4a"
        src.parent.mkdir(parents=True)
        _gen_loud_alac(src)
        source_lufs = _measure_lufs(src)
        assert source_lufs > -15.0, f"fixture not loud enough: {source_lufs}"

        conn = open_db(cfg.db_path)
        upsert_archive(
            conn,
            {
                "file_path": str(src),
                "filename": src.name,
                "ext": ".m4a",
                "status": "CATALOGUED",
                "codec": "alac",
                "artist": "Artist",
                "title": "Track",
            },
        )
        conn.commit()
        conn.close()

        result = _run_script(cfg, archive_dir, ["--execute"])
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "BAKED" in result.stdout

        expected_target = cfg.alac_library / "Artist" / "Album" / "track.m4a"
        assert expected_target.exists()
        # Source (the pristine archive copy) must be untouched -- never
        # modified or deleted by this script.
        assert src.exists()

        baked_lufs = _measure_lufs(expected_target)
        assert -20.0 <= baked_lufs <= -16.0, (
            f"baked output measured {baked_lufs} LUFS, expected ~-18"
        )

        conn = open_db(cfg.db_path)
        row = conn.execute(
            "SELECT file_path, lufs_baked_at, lufs_baked_target, status FROM archive"
        ).fetchone()
        events = conn.execute(
            "SELECT event_type FROM events WHERE event_type = 'LUFS_BAKE'"
        ).fetchall()
        conn.close()

        assert row["file_path"] == str(expected_target)
        assert row["lufs_baked_at"] is not None
        assert row["lufs_baked_target"] == -18.0
        assert row["status"] == "CATALOGUED"
        assert len(events) == 1

        # No leftover tmp/failed-verify artifacts.
        assert not list(cfg.alac_library.rglob("*.bake_tmp"))
        assert not list(cfg.alac_library.rglob("*.FAILED_VERIFY"))

    def test_second_run_skips_already_baked_row(self, cfg: MusicConfig) -> None:
        archive_dir = cfg.vault_root / "ALAC_Archive"
        archive_dir.mkdir(parents=True)
        src = archive_dir / "Artist" / "Album" / "track.m4a"
        src.parent.mkdir(parents=True)
        _gen_loud_alac(src)

        conn = open_db(cfg.db_path)
        upsert_archive(
            conn,
            {
                "file_path": str(src),
                "filename": src.name,
                "ext": ".m4a",
                "status": "CATALOGUED",
                "codec": "alac",
                "artist": "Artist",
                "title": "Track",
            },
        )
        conn.commit()
        conn.close()

        first = _run_script(cfg, archive_dir, ["--execute"])
        assert first.returncode == 0, first.stderr

        second = _run_script(cfg, archive_dir, ["--execute"])
        assert second.returncode == 0, second.stderr
        assert "0 row(s)" in second.stdout or "0 baked" in second.stdout


class TestUnmigratedWarning:
    """FinalizeStage doesn't write to ALAC_Archive yet (2026-08-18) --
    only migrate_to_archive.py moves content there, and only when someone
    remembers to run it. A finalized row still sitting in ALAC-Library is
    invisible to this script's own candidate query (it only reads rows
    already under archive_dir), so it needs to say so up front rather
    than silently bake nothing and look like there's simply no work."""

    def test_warns_when_finalized_rows_never_migrated(self, cfg: MusicConfig) -> None:
        archive_dir = cfg.vault_root / "ALAC_Archive"
        archive_dir.mkdir(parents=True)
        # A finalized row still in ALAC-Library -- never migrated.
        stuck = cfg.alac_library / "Artist" / "Album" / "stuck.m4a"
        stuck.parent.mkdir(parents=True)
        _gen_loud_alac(stuck)

        conn = open_db(cfg.db_path)
        upsert_archive(
            conn,
            {
                "file_path": str(stuck),
                "filename": stuck.name,
                "ext": ".m4a",
                "status": "CATALOGUED",
                "codec": "alac",
                "artist": "Artist",
                "title": "Stuck",
            },
        )
        conn.execute(
            "UPDATE archive SET finalized_at = datetime('now') WHERE file_path = ?",
            (str(stuck),),
        )
        conn.commit()
        conn.close()

        result = _run_script(cfg, archive_dir, [])
        assert result.returncode == 0, result.stderr
        assert "never migrated to ALAC_Archive" in result.stdout
        assert "migrate_to_archive.py" in result.stdout

    def test_no_warning_when_everything_migrated(self, cfg: MusicConfig) -> None:
        archive_dir = cfg.vault_root / "ALAC_Archive"
        archive_dir.mkdir(parents=True)
        src = archive_dir / "Artist" / "Album" / "track.m4a"
        src.parent.mkdir(parents=True)
        _gen_loud_alac(src)

        conn = open_db(cfg.db_path)
        upsert_archive(
            conn,
            {
                "file_path": str(src),
                "filename": src.name,
                "ext": ".m4a",
                "status": "CATALOGUED",
                "codec": "alac",
                "artist": "Artist",
                "title": "Track",
            },
        )
        conn.execute(
            "UPDATE archive SET finalized_at = datetime('now') WHERE file_path = ?",
            (str(src),),
        )
        conn.commit()
        conn.close()

        result = _run_script(cfg, archive_dir, [])
        assert result.returncode == 0, result.stderr
        assert "never migrated" not in result.stdout
