"""Review of slice E (#144), finding 8: `musaeus reset` and the console's
resets took no masters lock. Confirmed while a run or build had the database
open, they deleted it under that job, which went on writing into the deleted
file: everything after the snapshot was lost."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from musaeus import cli
from musaeus.config import MusicConfig
from musaeus.console import Console
from musaeus.db import open_db, upsert_archive
from musaeus.masters_lock import EXIT_BUSY, masters_lock


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
    c = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )
    c.ensure_dirs()
    conn = open_db(c.db_path)
    upsert_archive(conn, {"file_path": "/vault/a.m4a", "status": "CATALOGUED"})
    conn.commit()
    conn.close()
    return c


def _status(cfg: MusicConfig) -> str:
    conn = open_db(cfg.db_path)
    try:
        return conn.execute("SELECT status FROM archive").fetchone()[0]
    finally:
        conn.close()


def _no_snapshot(cfg: MusicConfig) -> bool:
    return not list(cfg.db_history_dir.glob("musaeus_pre_reset_*.db"))


def test_reset_refuses_while_a_build_holds_the_masters(cfg, monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_config", lambda: cfg)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "RESET")
    with masters_lock(cfg.runs_root, exclusive=False, what="edition-build car"):
        code = cli._cmd_reset()
    assert cfg.db_path.exists(), "the database was deleted under the build"
    assert _no_snapshot(cfg)
    assert code == EXIT_BUSY
    assert "edition-build car" in capsys.readouterr().err
    assert cli._cmd_reset() == 0, "free again: the reset goes ahead"
    assert not cfg.db_path.exists()


def _console(cfg: MusicConfig, monkeypatch, *answers: str) -> Console:
    con = Console()
    con._config = cfg
    replies = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(replies))
    return con


def test_hard_reset_refuses_while_a_run_holds_the_masters(cfg, monkeypatch):
    con = _console(cfg, monkeypatch, "3", "DELETE", "DELETE")  # 3 = Hard reset
    with masters_lock(cfg.runs_root, exclusive=True, what="musaeus pipeline (Forge)"):
        con._reset_menu()
    assert cfg.db_path.exists(), "the database was deleted under the run"
    assert _no_snapshot(cfg)


def test_soft_reset_refuses_while_a_run_holds_the_masters(cfg, monkeypatch):
    con = _console(cfg, monkeypatch, "1", "YES")  # 1 = Soft reset
    with masters_lock(cfg.runs_root, exclusive=True, what="musaeus pipeline (Forge)"):
        con._reset_menu()
    assert _status(cfg) == "CATALOGUED", "rows were re-queued under the run"
    con = _console(cfg, monkeypatch, "1", "YES")
    con._reset_menu()
    assert _status(cfg) == "PENDING", "free again: the soft reset goes ahead"
