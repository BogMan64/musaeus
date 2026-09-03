"""Organize must confirm its moves landed — and landed only once.

Moves are the costliest thing to get quietly wrong, and this stage has the
worst near-miss on record: on 2026-08-31 it would have flattened the
library, and after that fix would have re-merged DUPES_MOVED_FOR_REVIEW
back into it. Both were caught before either ran, by reading the code
rather than by anything the stage reported.

Two distinct failures are checked, because they look identical in a log
that only counts rows:

  - the file is NOT at the new path. The move reported success and did not
    happen, leaving the DB pointing at nothing. That is how a file ended up
    treated as its own duplicate (scope doc section 4.17).

  - the file is at BOTH paths. A "move" that copied looks perfectly fine
    per-row — the new path exists, the row is updated — and silently
    doubles the library. Nothing that counts successes can see it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from musaeus.stages.organize import OrganizeStage


class _Ctx:
    def __init__(self, conn, run_id="run_test"):
        self.conn = conn
        self.run_id = run_id


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE events (
        id INTEGER PRIMARY KEY, run_id TEXT, stage TEXT, event_type TEXT,
        old_value TEXT, new_value TEXT)""")
    return c


def _event(c, old, new, kind="ORGANIZE_MOVE", run="run_test"):
    c.execute("INSERT INTO events (run_id,stage,event_type,old_value,new_value) "
              "VALUES (?,?,?,?,?)", (run, "organize", kind, str(old), str(new)))
    c.commit()


def test_a_move_that_landed_verifies(conn, tmp_path: Path) -> None:
    old, new = tmp_path / "a.m4a", tmp_path / "Artist" / "b.m4a"
    new.parent.mkdir(parents=True)
    new.write_text("x")            # moved: only the new path exists
    _event(conn, old, new)
    assert OrganizeStage().verify_effect(_Ctx(conn), None) == []


def test_a_move_that_did_not_happen_is_reported(conn, tmp_path: Path) -> None:
    """The DB says it moved; the file is not there. This is the shape that
    leaves rows pointing at nothing."""
    old, new = tmp_path / "a.m4a", tmp_path / "gone" / "b.m4a"
    _event(conn, old, new)
    problems = OrganizeStage().verify_effect(_Ctx(conn), None)
    assert problems and "not at the new path" in problems[0]


def test_a_copy_masquerading_as_a_move_is_reported(conn, tmp_path: Path) -> None:
    """Looks fine per-row — new path exists, row updated — and doubles the
    library. Nothing that counts successes can see it."""
    old, new = tmp_path / "a.m4a", tmp_path / "Artist" / "a.m4a"
    new.parent.mkdir(parents=True)
    old.write_text("x")
    new.write_text("x")            # BOTH exist
    _event(conn, old, new)
    problems = OrganizeStage().verify_effect(_Ctx(conn), None)
    assert problems and "BOTH" in problems[0]


def test_renames_are_checked_too(conn, tmp_path: Path) -> None:
    old, new = tmp_path / "old.m4a", tmp_path / "new.m4a"
    _event(conn, old, new, kind="ORGANIZE_RENAME")
    assert OrganizeStage().verify_effect(_Ctx(conn), None)


def test_only_this_run_is_examined(conn, tmp_path: Path) -> None:
    """An earlier run's move that has since been superseded is not this
    run's failure."""
    old, new = tmp_path / "a.m4a", tmp_path / "missing.m4a"
    _event(conn, old, new, run="an_older_run")
    assert OrganizeStage().verify_effect(_Ctx(conn), None) == []


def test_no_moves_means_no_complaint(conn) -> None:
    assert OrganizeStage().verify_effect(_Ctx(conn), None) == []
