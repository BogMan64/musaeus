"""An aborted run must not look like a completed one.

Ctrl-C is a BaseException, so it skipped ctx.finish() and the run left no
RUN_END at all. Resume is per-ROW -- relaunching re-selects unfinished
rows and is safe -- but nothing recorded that a stage had never run. On
2026-08-26 a batch stopped after 22 of 27 stages; forge, tagger, audit,
enrich and mb_enrich were silently skipped, and the only way to know was
to read the stage list by hand.
"""

from __future__ import annotations

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext, StageResult
from musaeus.db import open_db


@pytest.fixture
def ctx(tmp_path) -> RunContext:
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )
    cfg.meta_dir.mkdir(parents=True, exist_ok=True)
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _end_note(conn, run_id) -> str | None:
    row = conn.execute(
        "SELECT note FROM events WHERE run_id=? AND event_type='RUN_END'", (run_id,)
    ).fetchone()
    return row["note"] if row else None


class TestRunEndRecordsAnAbort:
    def test_an_interrupted_run_is_marked_interrupted(self, ctx):
        import sqlite3

        ctx.stage_results.append(StageResult(stage_name="sentinel", success=True))
        run_id, path = ctx.run_id, ctx.config.db_path
        ctx.finish(interrupted=True)

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        note = _end_note(conn, run_id)
        assert note is not None, "an aborted run must still leave a RUN_END"
        assert "interrupted=True" in note
        # An interrupted run is not a successful one, however its stages went.
        assert "success=False" in note

    def test_a_completed_run_is_not_marked_interrupted(self, ctx):
        import sqlite3

        ctx.stage_results.append(StageResult(stage_name="sentinel", success=True))
        run_id, path = ctx.run_id, ctx.config.db_path
        ctx.finish()

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        note = _end_note(conn, run_id)
        assert "interrupted" not in note
        assert "success=True" in note


class TestTheNextLaunchSaysSo:
    def _console(self):
        from musaeus.console import Console

        return Console.__new__(Console)

    def test_a_run_without_run_end_is_reported(self, ctx, capsys):
        # A run that logged RUN_START and one stage, then vanished. Aged
        # past the "may still be live" window so this asserts the
        # abandoned-run path, not the in-flight one.
        ctx.log_event("STAGE_COMPLETE", stage="sentinel", note="sentinel: OK")
        ctx.conn.execute(
            "UPDATE events SET ts = datetime('now','-3 hours') WHERE run_id=?",
            (ctx.run_id,),
        )
        ctx.conn.commit()

        self._console()._warn_incomplete_previous_run(ctx.conn)

        out = capsys.readouterr().out
        assert "did not complete" in out
        assert ctx.run_id in out
        # and it must name what was skipped, not just that something was
        assert "Never ran:" in out
        assert "finalize" in out

    def test_a_completed_run_is_silent(self, ctx, capsys):
        ctx.log_event("STAGE_COMPLETE", stage="sentinel", note="ok")
        ctx.log_event("RUN_END", note="success=True stages=27")
        ctx.conn.commit()

        self._console()._warn_incomplete_previous_run(ctx.conn)

        assert capsys.readouterr().out == ""

    def test_an_empty_database_is_silent(self, tmp_path, capsys):
        # A bare DB, not the ctx fixture: RunContext.new() logs RUN_START,
        # so a context of its own would count as an incomplete run. In
        # production the check runs BEFORE the run is created, which is
        # what this reproduces.
        conn = open_db(tmp_path / "empty.db")
        self._console()._warn_incomplete_previous_run(conn)
        assert capsys.readouterr().out == ""

    def test_it_does_not_report_the_run_that_is_about_to_start(self, ctx, capsys):
        # The ordering guarantee, asserted rather than assumed: the check
        # is called before RunContext.new(), so the only RUN_START it can
        # see belongs to an earlier run. Here one exists and IS incomplete,
        # so it must be reported -- the current run has not been created.
        ctx.conn.execute(
            "UPDATE events SET ts = datetime('now','-3 hours') WHERE run_id=?",
            (ctx.run_id,),
        )
        ctx.conn.commit()
        self._console()._warn_incomplete_previous_run(ctx.conn)
        out = capsys.readouterr().out
        assert ctx.run_id in out


class TestARunStillInFlightIsNotCalledFailed:
    """preflight blocks on an interactive [y/N] before doing any work, so a
    run can sit with only RUN_START for minutes while perfectly healthy.
    Announcing that as "did not complete" is a warning that cries wolf.
    """

    def _console(self):
        from musaeus.console import Console

        return Console.__new__(Console)

    def test_a_recent_unfinished_run_is_flagged_as_maybe_live(self, ctx, capsys):
        # ctx has just logged RUN_START, so it is seconds old.
        self._console()._warn_incomplete_previous_run(ctx.conn)
        out = capsys.readouterr().out
        assert "may still be running" in out
        assert "did not complete" not in out

    def test_an_old_unfinished_run_is_reported_as_incomplete(self, ctx, capsys):
        ctx.conn.execute(
            "UPDATE events SET ts = datetime('now','-3 hours') WHERE run_id=?",
            (ctx.run_id,),
        )
        ctx.conn.commit()

        self._console()._warn_incomplete_previous_run(ctx.conn)
        out = capsys.readouterr().out
        assert "did not complete" in out
        assert "Never ran:" in out
