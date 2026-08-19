"""
Tests for Console._reset_menu's hard-reset path -- specifically that it
now snapshots musaeus.db before deleting it (2026-08-18 fix; previously
documented as intended behavior in config.db_history_dir's own
docstring, but neither reset code path -- this one or cli.py's
_cmd_reset -- actually called it).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.console import Console
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


def _console_with_config(cfg: MusicConfig) -> Console:
    con = Console()
    con._config = cfg
    return con


class TestHardResetSnapshots:
    def test_hard_reset_snapshots_before_deleting(self, cfg: MusicConfig, monkeypatch):
        cfg.ensure_dirs()
        conn = open_db(cfg.db_path)
        upsert_archive(conn, {"file_path": "/vault/a.m4a", "status": "CATALOGUED"})
        conn.commit()
        conn.close()

        con = _console_with_config(cfg)
        responses = iter(["1", "DELETE", "DELETE"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))

        con._reset_menu()

        assert not cfg.db_path.exists(), "hard reset should still delete the live DB"

        snapshots = list(cfg.db_history_dir.glob("musaeus_pre_reset_*.db"))
        assert len(snapshots) == 1, f"expected exactly one snapshot, found {snapshots}"

        import sqlite3

        snap_conn = sqlite3.connect(str(snapshots[0]))
        row = snap_conn.execute("SELECT file_path FROM archive").fetchone()
        snap_conn.close()
        assert row[0] == "/vault/a.m4a"

    def test_cancelling_confirmation_leaves_db_and_history_untouched(
        self, cfg: MusicConfig, monkeypatch
    ):
        cfg.ensure_dirs()
        conn = open_db(cfg.db_path)
        conn.close()

        con = _console_with_config(cfg)
        responses = iter(["1", "DELETE", "not-delete"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))

        con._reset_menu()

        assert cfg.db_path.exists(), "cancelling the second confirmation must not delete anything"
        assert not cfg.db_history_dir.exists() or not list(
            cfg.db_history_dir.glob("musaeus_pre_reset_*.db")
        ), "cancelled reset must not have snapshotted either"

    def test_soft_reset_never_snapshots(self, cfg: MusicConfig, monkeypatch):
        """Soft reset only UPDATEs rows back to PENDING -- it never deletes
        the DB file, so there's nothing to snapshot and it shouldn't try."""
        cfg.ensure_dirs()
        conn = open_db(cfg.db_path)
        upsert_archive(conn, {"file_path": "/vault/a.m4a", "status": "CATALOGUED"})
        conn.commit()
        conn.close()

        con = _console_with_config(cfg)
        responses = iter(["0", "YES"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))

        con._reset_menu()

        assert cfg.db_path.exists()
        assert not cfg.db_history_dir.exists() or not list(
            cfg.db_history_dir.glob("musaeus_pre_reset_*.db")
        )
