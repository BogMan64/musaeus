"""The loop detector, which is the only part of the convergence report that
can be wrong in a way nobody would notice.

Grey's plan is to run the pipeline repeatedly until it stops changing
things. The report exists to tell convergence from oscillation, and the
difference is invisible from the change count alone: a stage renaming A to
B while another renames B back to A reports work on every pass forever.

So the detector is tested for both errors, not one. Missing a real loop
means the plan runs until Grey notices by hand. Inventing a loop that is
not there means he stops a run that was working.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "convergence_report",
    Path(__file__).resolve().parent.parent / "scripts" / "convergence_report.py",
)
cr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cr)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts TEXT, "
        "event_type TEXT, file_path TEXT, old_value TEXT, new_value TEXT, stage TEXT, note TEXT)"
    )
    return c


def add(conn, run, etype, path, old, new, ts="2026-09-09 00:00:00"):
    conn.execute(
        "INSERT INTO events (run_id, ts, event_type, file_path, old_value, new_value) "
        "VALUES (?,?,?,?,?,?)",
        (run, ts, etype, path, old, new),
    )


class TestBucket:
    def test_a_before_and_a_different_after_is_a_change(self):
        assert cr.bucket("Rock", "Pop") == cr.TRANSITION

    def test_a_before_equal_to_the_after_changed_nothing(self):
        """A stage that rewrote the same value did not change the library.
        Counting it would keep the change count off zero forever."""
        assert cr.bucket("Rock", "Rock") == cr.OBSERVATION

    def test_a_write_with_no_recorded_prior_is_blind_not_a_change(self):
        """TAGGER_WRITE's shape. Something was written; whether it differed
        is not knowable from the event, and must not be guessed either way."""
        assert cr.bucket("", "Rock") == cr.BLIND
        assert cr.bucket(None, "Rock") == cr.BLIND

    def test_neither_recorded_is_an_observation(self):
        assert cr.bucket("", "") == cr.OBSERVATION
        assert cr.bucket(None, None) == cr.OBSERVATION

    def test_whitespace_is_not_a_value(self):
        assert cr.bucket("   ", "Rock") == cr.BLIND


class TestOscillation:
    def test_a_value_that_comes_back_is_a_loop(self):
        """The case the report exists for: A -> B, then B -> A."""
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, "
            "ts TEXT, event_type TEXT, file_path TEXT, old_value TEXT, new_value TEXT)"
        )
        add(c, "run_1", "TAG", "/x/a.m4a", "The Beatles", "Beatles, The")
        add(c, "run_2", "TAG", "/x/a.m4a", "Beatles, The", "The Beatles")
        osc = cr.find_oscillations(c, ["run_1", "run_2"])
        assert len(osc) == 1
        assert osc[0]["file_path"] == "/x/a.m4a"
        assert "The Beatles" in osc[0]["value_cycle"]

    def test_steady_progress_is_not_a_loop(self, conn):
        """A -> B -> C is three different values and settles. Flagging it
        would stop a run that was working."""
        add(conn, "run_1", "TAG", "/x/a.m4a", "beatles", "Beatles")
        add(conn, "run_2", "TAG", "/x/a.m4a", "Beatles", "The Beatles")
        assert cr.find_oscillations(conn, ["run_1", "run_2"]) == []

    def test_one_change_is_never_a_loop(self, conn):
        add(conn, "run_1", "TAG", "/x/a.m4a", "a", "b")
        assert cr.find_oscillations(conn, ["run_1"]) == []

    def test_two_fields_changing_are_two_stories_not_a_loop(self, conn):
        """A file whose artist AND genre both change is not oscillating.
        Keying on the file alone would invent a loop out of two unrelated
        edits that happen to share a path."""
        add(conn, "run_1", "ARTIST", "/x/a.m4a", "a", "b")
        add(conn, "run_2", "GENRE", "/x/a.m4a", "b", "a")
        assert cr.find_oscillations(conn, ["run_1", "run_2"]) == []

    def test_a_longer_cycle_is_still_caught(self, conn):
        """A -> B -> C -> A takes three passes to close and still loops."""
        add(conn, "run_1", "TAG", "/x/a.m4a", "A", "B")
        add(conn, "run_2", "TAG", "/x/a.m4a", "B", "C")
        add(conn, "run_3", "TAG", "/x/a.m4a", "C", "A")
        osc = cr.find_oscillations(conn, ["run_1", "run_2", "run_3"])
        assert len(osc) == 1
        assert osc[0]["passes"] == 3

    def test_blind_writes_cannot_produce_a_loop(self, conn):
        """They record no prior value, so a returning value is not visible
        in them. Better to report nothing than to guess a cycle."""
        add(conn, "run_1", "TAGGER_WRITE", "/x/a.m4a", "", "Rock")
        add(conn, "run_2", "TAGGER_WRITE", "/x/a.m4a", "", "Pop")
        add(conn, "run_3", "TAGGER_WRITE", "/x/a.m4a", "", "Rock")
        assert cr.find_oscillations(conn, ["run_1", "run_2", "run_3"]) == []

    def test_events_with_no_file_path_are_skipped(self, conn):
        """Run-level events share the empty path and would otherwise all
        look like one wildly oscillating file."""
        add(conn, "run_1", "STAGE", "", "a", "b")
        add(conn, "run_2", "STAGE", "", "b", "a")
        assert cr.find_oscillations(conn, ["run_1", "run_2"]) == []


class TestRunSelection:
    def test_only_pipeline_runs_count_as_passes(self, conn):
        """One-off repair scripts stamp their own run_id. Counting them as
        passes would put a hand-made delete in the convergence trend."""
        add(conn, "run_20260909T010000Z_abc", "TAG", "/x/a.m4a", "a", "b", ts="2026-09-09 01:00")
        add(conn, "grey_delete_20260909", "DELETE", "/x/b.m4a", "a", "b", ts="2026-09-09 02:00")
        add(conn, "phantom_dupe_clear_2026", "X", "/x/c.m4a", "a", "b", ts="2026-09-09 03:00")
        runs = cr.pipeline_runs(conn, 10)
        assert [r[0] for r in runs] == ["run_20260909T010000Z_abc"]

    def test_runs_come_back_oldest_first(self, conn):
        """The trend reads left to right, so the order is load-bearing."""
        add(conn, "run_b", "TAG", "/x/a.m4a", "a", "b", ts="2026-09-09 02:00")
        add(conn, "run_a", "TAG", "/x/a.m4a", "a", "b", ts="2026-09-09 01:00")
        assert [r[0] for r in cr.pipeline_runs(conn, 10)] == ["run_a", "run_b"]
