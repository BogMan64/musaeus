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


def test_an_interrupted_console_run_keeps_its_records(con, monkeypatch):
    """Pre-merge review of #168: a console run cut short after Finalize kept
    no records; the CLI keeps them for exactly that case (2026-09-25)."""
    from musaeus import cli

    called = []
    monkeypatch.setattr(cli, "_end_of_run_records", lambda *a: called.append(a) or True)

    class Stop(GhostStage):
        def execute(self, ctx):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        con._run_pipeline(dry_run=False, stage_classes=[Stop], label="Stop")
    assert called, "the interrupted run wrote no records"


@pytest.mark.parametrize(("current", "order"), [(False, ["drift"]), (True, ["drift", "lock"])])
def test_the_source_check_runs_before_the_masters_lock(con, monkeypatch, current, order):
    """Grey's code review, 2026-10-11, finding 10: the check ran under the
    lock, and its restart left the lock's holder record naming a live pid that
    held nothing."""
    import contextlib

    from musaeus import masters_lock

    seen: list[str] = []

    @contextlib.contextmanager
    def lock(*a, **k):
        seen.append("lock")
        yield

    def drift():
        seen.append("drift")
        return current

    monkeypatch.setattr(masters_lock, "masters_lock", lock)
    monkeypatch.setattr(con, "_check_source_drift", drift)
    # The run itself stops at its database (where main ran the check, after
    # the lock), so the test sees the real order on both versions.
    monkeypatch.setattr(con, "_open_db", lambda: seen.append("db"))

    con._run_pipeline(dry_run=False)

    assert seen == (order + ["db"] if current else order)


@pytest.mark.parametrize("entry", ["_run_stage", "_run_stage_with_stash"])
def test_a_live_single_stage_checks_for_changed_source(con, monkeypatch, entry):
    """Pre-merge review of #186: a single live stage never checked, so a
    console left open across a fix ran Finalize or Forge on the old code."""
    from musaeus.stages import FinalizeStage

    def stale():
        return False

    ran: list[str] = []
    monkeypatch.setattr(con, "_check_source_drift", stale)
    monkeypatch.setattr(con, "_masters_held", lambda *a, **k: ran.append("ran"))

    getattr(con, entry)(FinalizeStage, False)

    assert ran == [], "a live stage ran on stale code"


def test_interactive_dedupe_checks_for_changed_source(con, monkeypatch):
    """Pre-merge review of #186: interactive dedupe carries choices out at
    once (it moves files) and never checked for stale code."""
    from musaeus import console as console_mod

    opened: list[str] = []
    monkeypatch.setattr(console_mod, "_choose", lambda *a, **k: "0")
    monkeypatch.setattr(con, "_check_source_drift", lambda: False)
    monkeypatch.setattr(con, "_open_db", lambda: opened.append("db"))

    con._run_dedupe()

    assert opened == [], "interactive dedupe opened the masters' database on stale code"
