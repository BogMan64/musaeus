"""Review of slice E (#144), findings 9 and 10.

9: the validation_issues re-key ran through executescript, one commit per
statement, and created validation_issues__new without IF NOT EXISTS. Cut
short, every later open failed, or the issue history was stranded in __new.

10: open_db's migrations and ensure_columns looked for a column and then added
it with no lock. Two processes opening an older database at once could both
add it; the second failed on "duplicate column name".
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

import pytest

from musaeus import db
from musaeus.db import _validation_issues_key_includes_run_id, ensure_columns, open_db

_OLD_KEY = """
CREATE TABLE validation_issues (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path   TEXT NOT NULL,
    issue       TEXT NOT NULL,
    severity    TEXT DEFAULT 'warning',
    run_id      TEXT,
    checked_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(file_path, issue, run_id)
);
"""
_COPY = "CREATE TABLE validation_issues__new (" + _OLD_KEY.split("(", 1)[1].replace(
    "UNIQUE(file_path, issue, run_id)", "UNIQUE(file_path, issue)"
)
HISTORY = [
    ("/x/a.m4a", "NO_GENRE", "warning", "r1", "2026-08-01"),
    ("/x/a.m4a", "NO_GENRE", "error", "r2", "2026-08-29"),
    ("/x/b.m4a", "NO_YEAR", "warning", "r1", "2026-08-01"),
]


def _insert(conn, table, rows):
    conn.executemany(
        f"INSERT INTO {table} (file_path, issue, severity, run_id, checked_at) VALUES (?,?,?,?,?)",
        rows,
    )


def _old_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(_OLD_KEY)
    _insert(conn, "validation_issues", HISTORY)
    conn.commit()
    return conn


def _issues(path):
    """What an open finds: the issues, and whether the re-key is finished."""
    conn = open_db(path)
    try:
        rows = sorted(
            tuple(r)
            for r in conn.execute("SELECT file_path, issue, severity FROM validation_issues")
        )
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finished = (
            not _validation_issues_key_includes_run_id(conn)
            and "validation_issues__new" not in tables
        )
        return rows, finished
    finally:
        conn.close()


COLLAPSED = [("/x/a.m4a", "NO_GENRE", "error"), ("/x/b.m4a", "NO_YEAR", "warning")]


def test_a_rekey_cut_short_leaves_the_old_table_whole(tmp_path):
    path = tmp_path / "m.db"
    conn = _old_db(path)

    def stop_at_the_rename(action, *args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ALTER_TABLE else sqlite3.SQLITE_OK

    conn.set_authorizer(stop_at_the_rename)
    with pytest.raises(sqlite3.DatabaseError):
        db._rebuild_validation_issues(conn)
    conn.close()
    rows, finished = _issues(path)
    assert rows == COLLAPSED, "the issue history was lost"
    assert finished


def test_an_open_after_the_old_code_stopped_before_the_drop(tmp_path):
    path = tmp_path / "m.db"
    conn = _old_db(path)
    conn.executescript(_COPY)  # the copy made, the old table still whole
    _insert(conn, "validation_issues__new", HISTORY[:1])
    conn.commit()
    conn.close()
    rows, finished = _issues(path)
    assert rows == COLLAPSED
    assert finished


def test_an_open_after_the_old_code_stopped_after_the_drop(tmp_path):
    path = tmp_path / "m.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_COPY)  # the history copied, the old table dropped
    _insert(conn, "validation_issues__new", [HISTORY[1], HISTORY[2]])
    # The old code's next open made an empty table; a run since saw one again.
    conn.executescript(_COPY.replace("validation_issues__new", "validation_issues"))
    _insert(conn, "validation_issues", [("/x/b.m4a", "NO_YEAR", "error", "r3", "2026-10-01")])
    conn.commit()
    conn.close()
    rows, finished = _issues(path)
    assert rows == [("/x/a.m4a", "NO_GENRE", "error"), ("/x/b.m4a", "NO_YEAR", "error")]
    assert finished


class _Raced(sqlite3.Connection):
    """Another process adds the column just before this one starts to."""

    rival: Callable[[], None] | None = None

    def execute(self, sql, *args):  # type: ignore[override]
        if self.rival and sql.lstrip().upper().startswith(("BEGIN IMMEDIATE", "ALTER TABLE")):
            rival, self.rival = self.rival, None
            rival()
        return super().execute(sql, *args)


def _raced(path, table, col, decl):
    def add():
        other = sqlite3.connect(str(path))
        other.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        other.commit()
        other.close()

    conn = sqlite3.connect(str(path), factory=_Raced, timeout=5)
    conn.rival = add
    return conn


def test_ensure_columns_when_another_process_adds_it_first(tmp_path):
    path = tmp_path / "m.db"
    open_db(path).close()
    conn = _raced(path, "archive", "x_added", "TEXT")
    assert ensure_columns(conn, [("x_added", "TEXT")]) == 0
    assert "x_added" in {r[1] for r in conn.execute("PRAGMA table_info(archive)")}


def test_open_db_when_another_process_migrates_first(tmp_path):
    path = tmp_path / "m.db"
    open_db(path).close()
    table, col, decl = db._MIGRATIONS[-1]
    conn = sqlite3.connect(str(path))
    conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")  # an older database
    conn.commit()
    conn.close()
    conn = _raced(path, table, col, decl)
    db._apply_migrations(conn)
    assert col in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
