"""
Regression tests for musaeus/console.py's DB-connection lifecycle.

Focus: the 2026-08-11 bug where a KeyboardInterrupt during a live stage
run leaked the DB connection opened by _run_stage/_run_stage_with_stash/
_run_pipeline. Those methods only closed the connection in their
`except Exception:` handler or implicitly via ctx.finish() on success --
neither path runs for a KeyboardInterrupt, which is a BaseException, not
an Exception, so a Ctrl+C during a live stage skipped cleanup entirely.

These tests verify the fix directly -- was conn.close() actually called
on every exit path -- rather than through a live SQLITE_BUSY collision.
A lock-contention-based test was tried first and discarded: it couldn't
reliably discriminate the buggy version from the fixed one in an
isolated test process, because CPython's refcounting GC closes an
abandoned sqlite3.Connection (releasing its OS-level lock) as soon as
nothing references it, which happens quickly once a raised exception is
fully caught and its traceback dropped -- a timing detail specific to a
short, isolated test process. The real incident's connection stayed
locked for the rest of a long-lived interactive console session; a
direct call-tracking assertion is deterministic regardless of GC timing
in either environment. sqlite3.Connection is a C extension type and
cannot be monkeypatched directly (`.close` is a read-only attribute), so
the tracker wraps the real connection instead of patching it in place.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.console import Console
from musaeus.context import RunContext, StageResult
from musaeus.stages.base import BaseStage


class _CloseTrackingConn:
    """Transparent wrapper around a real sqlite3.Connection that records
    whether .close() was ever called, while delegating everything else
    (execute, commit, row_factory, ...) to the real connection."""

    def __init__(self, real_conn):
        object.__setattr__(self, "_real", real_conn)
        object.__setattr__(self, "close_called", False)

    def close(self):
        object.__setattr__(self, "close_called", True)
        self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        setattr(self._real, name, value)


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


@pytest.fixture
def console(cfg: MusicConfig, monkeypatch) -> tuple[Console, list[_CloseTrackingConn]]:
    """A Console wired to a disposable DB, whose _open_db() is wrapped
    so every connection it hands out is close-tracked. Returns
    (console, tracked_connections) -- tests inspect the list rather than
    a single connection, since a stage can in principle open more than
    one over its lifetime."""
    cfg.ensure_dirs()
    c = Console()
    c._config = cfg  # bypass interactive _boot_check()

    tracked: list[_CloseTrackingConn] = []
    real_open_db = c._open_db

    def tracking_open_db(dry_run: bool = False):
        conn = real_open_db(dry_run=dry_run)
        if conn is None:
            return None
        wrapper = _CloseTrackingConn(conn)
        tracked.append(wrapper)
        return wrapper

    monkeypatch.setattr(c, "_open_db", tracking_open_db)
    return c, tracked


class _InterruptingStage(BaseStage):
    """A stage whose run()/dry_run() raises KeyboardInterrupt, simulating
    Ctrl+C during a live stage -- exactly what triggered the real bug."""

    NAME = "interrupting"

    def validate(self, ctx: RunContext) -> None:
        pass

    def run(self, ctx: RunContext) -> StageResult:
        raise KeyboardInterrupt()

    def dry_run(self, ctx: RunContext) -> StageResult:
        raise KeyboardInterrupt()


class _CrashingStage(BaseStage):
    """A stage that raises a normal exception -- the already-correctly-
    handled path, kept here as a regression guard against the refactor
    breaking what already worked."""

    NAME = "crashing"

    def validate(self, ctx: RunContext) -> None:
        pass

    def run(self, ctx: RunContext) -> StageResult:
        raise RuntimeError("boom")

    def dry_run(self, ctx: RunContext) -> StageResult:
        raise RuntimeError("boom")


class TestConnectionLeakOnInterrupt:
    def test_run_stage_closes_connection_on_keyboard_interrupt(self, console):
        con, tracked = console
        with pytest.raises(KeyboardInterrupt):
            con._run_stage(_InterruptingStage, dry_run=False)
        assert tracked, "no connection was opened"
        assert tracked[-1].close_called, "connection was never closed after KeyboardInterrupt"

    def test_run_stage_with_stash_closes_connection_on_keyboard_interrupt(self, console):
        con, tracked = console
        with pytest.raises(KeyboardInterrupt):
            con._run_stage_with_stash(_InterruptingStage, dry_run=False)
        assert tracked, "no connection was opened"
        assert tracked[-1].close_called, "connection was never closed after KeyboardInterrupt"

    def test_run_pipeline_closes_connection_on_keyboard_interrupt(self, console, monkeypatch):
        con, tracked = console
        import musaeus.console as console_mod

        monkeypatch.setattr(console_mod, "DEFAULT_PIPELINE", [_InterruptingStage])

        with pytest.raises(KeyboardInterrupt):
            con._run_pipeline(dry_run=False)
        assert tracked, "no connection was opened"
        assert tracked[-1].close_called, "connection was never closed after KeyboardInterrupt"

    def test_run_stage_still_closes_connection_on_normal_exception(self, console):
        """Regression guard: a plain exception was already handled
        correctly before this fix -- confirm it still is."""
        con, tracked = console
        con._run_stage(_CrashingStage, dry_run=False)  # caught internally, no raise
        assert tracked, "no connection was opened"
        assert tracked[-1].close_called

    def test_run_stage_with_stash_still_closes_connection_on_normal_exception(self, console):
        con, tracked = console
        con._run_stage_with_stash(_CrashingStage, dry_run=False)
        assert tracked, "no connection was opened"
        assert tracked[-1].close_called

    def test_run_pipeline_still_closes_connection_on_normal_exception(self, console, monkeypatch):
        con, tracked = console
        import musaeus.console as console_mod

        monkeypatch.setattr(console_mod, "DEFAULT_PIPELINE", [_CrashingStage])
        con._run_pipeline(dry_run=False)  # caught internally, no raise
        assert tracked, "no connection was opened"
        assert tracked[-1].close_called

    def test_run_stage_closes_connection_on_success(self, console):
        """Success path already worked (via ctx.finish()) -- confirm the
        refactor didn't change that."""

        class _QuietStage(BaseStage):
            NAME = "quiet"

            def validate(self, ctx: RunContext) -> None:
                pass

            def run(self, ctx: RunContext) -> StageResult:
                result = self._make_result(dry_run=False)
                ctx.record_stage(result)
                return result

            def dry_run(self, ctx: RunContext) -> StageResult:
                return self.run(ctx)

        con, tracked = console
        con._run_stage(_QuietStage, dry_run=False)
        assert tracked, "no connection was opened"
        assert tracked[-1].close_called
