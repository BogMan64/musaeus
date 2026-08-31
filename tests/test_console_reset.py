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
        responses = iter(["2", "DELETE", "DELETE"])
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
        responses = iter(["2", "DELETE", "not-delete"])
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


class TestSoftResetPreservesDecisions:
    """The soft reset's UPDATE had no WHERE clause and set EVERY row to
    PENDING -- including QUARANTINED and DUPE_REVIEW (judgements Grey made)
    and GHOST (a claim that the file is gone from disk).

    GHOST is the sharpest case: PENDING means "not yet processed", so
    resetting a GHOST row does not merely forget a decision, it asserts
    something false and leaves the pipeline to rediscover the truth by
    failing to find the file. The reset also nulls audio_hash, which on a
    GHOST row is the last surviving record of what the file was.

    Restricted 2026-08-31 on Grey's instruction, with the unrestricted
    behaviour kept as a separately named menu option.
    """

    def _seeded(self, cfg: MusicConfig):
        cfg.ensure_dirs()
        conn = open_db(cfg.db_path)
        for path, status in (
            ("/vault/catalogued.m4a", "CATALOGUED"),
            ("/vault/quarantined.m4a", "QUARANTINED"),
            ("/vault/dupe.m4a", "DUPE_REVIEW"),
            ("/vault/ghost.m4a", "GHOST"),
        ):
            upsert_archive(conn, {"file_path": path, "status": status,
                                  "audio_hash": "deadbeef"})
        conn.commit()
        conn.close()

    def _statuses(self, cfg: MusicConfig) -> dict[str, str]:
        conn = open_db(cfg.db_path)
        rows = conn.execute("SELECT file_path, status FROM archive").fetchall()
        conn.close()
        return {r["file_path"]: r["status"] for r in rows}

    def test_default_soft_reset_leaves_decisions_alone(self, cfg, monkeypatch):
        self._seeded(cfg)
        con = _console_with_config(cfg)
        responses = iter(["0", "YES"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))

        con._reset_menu()

        st = self._statuses(cfg)
        assert st["/vault/catalogued.m4a"] == "PENDING", "the ordinary case must reset"
        assert st["/vault/quarantined.m4a"] == "QUARANTINED"
        assert st["/vault/dupe.m4a"] == "DUPE_REVIEW"
        assert st["/vault/ghost.m4a"] == "GHOST"

    def test_ghost_keeps_its_audio_hash(self, cfg, monkeypatch):
        """audio_hash on a GHOST row is the last record of what the file was."""
        self._seeded(cfg)
        con = _console_with_config(cfg)
        responses = iter(["0", "YES"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))

        con._reset_menu()

        conn = open_db(cfg.db_path)
        h = conn.execute(
            "SELECT audio_hash FROM archive WHERE file_path='/vault/ghost.m4a'"
        ).fetchone()[0]
        conn.close()
        assert h == "deadbeef"

    def test_including_decisions_resets_everything(self, cfg, monkeypatch):
        """The blunt instrument still exists -- it just has to be asked for."""
        self._seeded(cfg)
        con = _console_with_config(cfg)
        responses = iter(["1", "RESET-ALL"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))

        con._reset_menu()

        assert set(self._statuses(cfg).values()) == {"PENDING"}

    def test_including_decisions_requires_its_own_confirmation_word(self, cfg, monkeypatch):
        """Typing the ordinary YES must not trigger the destructive variant."""
        self._seeded(cfg)
        con = _console_with_config(cfg)
        responses = iter(["1", "YES"])
        monkeypatch.setattr("builtins.input", lambda *a, **k: next(responses))

        con._reset_menu()

        st = self._statuses(cfg)
        assert st["/vault/quarantined.m4a"] == "QUARANTINED", "YES must not arm RESET-ALL"
        assert st["/vault/catalogued.m4a"] == "CATALOGUED", "cancel must change nothing"
