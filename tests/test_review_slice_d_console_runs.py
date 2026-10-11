"""Review of slice D (#146), findings 6 and 7: the console's runs.

6. Console live runs kept none of a CLI run's records: no run log, no
   handoff doc, no problem list, nothing beside the library.
7. Console Preview executed the stages with a flag set -- the path the CLI
   retired -- writing events and adding archive columns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.console import Console
from musaeus.db import open_db, upsert_archive
from musaeus.stages import GhostStage


@pytest.fixture
def con(tmp_path: Path, monkeypatch) -> Console:
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
    conn = open_db(cfg.db_path)
    upsert_archive(conn, {"file_path": str(tmp_path / "a.m4a"), "status": "PENDING"})
    conn.commit()
    conn.close()
    c = Console()
    c._config = cfg
    monkeypatch.setattr(c, "_check_source_drift", lambda: True)
    return c


def _snapshot(db: Path):
    conn = open_db(db)
    try:
        events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        cols = [r[1] for r in conn.execute("PRAGMA table_info(archive)")]
        return events, cols
    finally:
        conn.close()


def test_a_console_preview_writes_nothing(con):
    before = _snapshot(con._config.db_path)
    con._run_pipeline(dry_run=True)
    assert _snapshot(con._config.db_path) == before, "the preview wrote to the database"


def test_a_console_live_run_keeps_its_log(con):
    con._run_pipeline(dry_run=False, stage_classes=[GhostStage], label="Ghost")
    logs = list((con._config.runs_root / "LOGS").glob("run_*.log"))
    assert logs, "the console run left no run log"
    assert "ghost" in logs[0].read_text(encoding="utf-8").lower()
