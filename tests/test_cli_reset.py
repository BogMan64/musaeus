"""
Tests for cli.py's _cmd_reset -- specifically that it now snapshots
musaeus.db before deleting it (2026-08-18 fix; previously documented as
intended behavior in config.db_history_dir's own docstring, but neither
reset code path -- this one or console.py's _reset_menu hard reset --
actually called it).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from musaeus import cli
from musaeus.config import MusicConfig
from musaeus.db import open_db, upsert_archive


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


class TestCmdResetSnapshots:
    def test_snapshots_before_deleting(self, cfg: MusicConfig, monkeypatch):
        cfg.ensure_dirs()
        conn = open_db(cfg.db_path)
        upsert_archive(conn, {"file_path": "/vault/a.m4a", "status": "CATALOGUED"})
        conn.commit()
        conn.close()

        monkeypatch.setattr(cli, "get_config", lambda: cfg)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "RESET")

        cli._cmd_reset()

        assert not cfg.db_path.exists()
        snapshots = list(cfg.db_history_dir.glob("musaeus_pre_reset_*.db"))
        assert len(snapshots) == 1

        snap_conn = sqlite3.connect(str(snapshots[0]))
        row = snap_conn.execute("SELECT file_path FROM archive").fetchone()
        snap_conn.close()
        assert row[0] == "/vault/a.m4a"

    def test_no_tty_refuses_and_does_not_snapshot(self, cfg: MusicConfig, monkeypatch):
        cfg.ensure_dirs()
        conn = open_db(cfg.db_path)
        conn.close()

        monkeypatch.setattr(cli, "get_config", lambda: cfg)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

        cli._cmd_reset()

        assert cfg.db_path.exists(), "no-TTY must refuse before touching anything"
        assert not cfg.db_history_dir.exists() or not list(
            cfg.db_history_dir.glob("musaeus_pre_reset_*.db")
        )

    def test_wrong_confirmation_does_not_snapshot_or_delete(self, cfg: MusicConfig, monkeypatch):
        cfg.ensure_dirs()
        conn = open_db(cfg.db_path)
        conn.close()

        monkeypatch.setattr(cli, "get_config", lambda: cfg)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "not reset")

        cli._cmd_reset()

        assert cfg.db_path.exists()
        assert not cfg.db_history_dir.exists() or not list(
            cfg.db_history_dir.glob("musaeus_pre_reset_*.db")
        )
