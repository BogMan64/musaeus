"""validation_issues must not breed a row per run.

The table reached 343,938 rows and was pruned to 15,604 on 2026-08-24, then
regrew 30-50k per run -- because UNIQUE(file_path, issue, run_id) makes "the
same problem, seen again" a brand-new row every time. The prune treated the
symptom; the key is the cause.
"""

from __future__ import annotations

import sqlite3

from musaeus.db import (
    _rebuild_validation_issues,
    _validation_issues_key_includes_run_id,
    open_db,
)

_OLD_SCHEMA = """
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


def _legacy_db(path, rows):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_OLD_SCHEMA)
    conn.executemany(
        "INSERT INTO validation_issues (file_path, issue, severity, run_id, checked_at) "
        "VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return conn


# ── detection ─────────────────────────────────────────────────────────────────


def test_the_old_key_is_detected(tmp_path):
    conn = _legacy_db(tmp_path / "old.db", [])
    assert _validation_issues_key_includes_run_id(conn)


def test_a_fresh_db_is_already_correct(tmp_path):
    conn = open_db(tmp_path / "new.db")
    assert not _validation_issues_key_includes_run_id(conn)


def test_detection_is_safe_when_the_table_is_absent(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "empty.db"))
    assert not _validation_issues_key_includes_run_id(conn)


# ── the rebuild ───────────────────────────────────────────────────────────────


def test_duplicates_across_runs_collapse_to_one_row(tmp_path):
    """Five runs finding the same problem is one problem, not five rows."""
    rows = [
        ("/x/a.m4a", "NO_GENRE", "warning", f"run_{i}", f"2026-08-2{i} 00:00:00")
        for i in range(1, 6)
    ]
    conn = _legacy_db(tmp_path / "old.db", rows)
    assert conn.execute("SELECT COUNT(*) FROM validation_issues").fetchone()[0] == 5

    removed = _rebuild_validation_issues(conn)

    assert removed == 4
    assert conn.execute("SELECT COUNT(*) FROM validation_issues").fetchone()[0] == 1


def test_the_surviving_row_is_the_most_recent_sighting(tmp_path):
    """Keeping the FIRST sighting would make the table permanently stale."""
    rows = [
        ("/x/a.m4a", "NO_GENRE", "warning", "run_old", "2026-08-01 00:00:00"),
        ("/x/a.m4a", "NO_GENRE", "error", "run_new", "2026-08-29 00:00:00"),
    ]
    conn = _legacy_db(tmp_path / "old.db", rows)
    _rebuild_validation_issues(conn)

    row = conn.execute("SELECT * FROM validation_issues").fetchone()
    assert row["run_id"] == "run_new"
    assert row["checked_at"] == "2026-08-29 00:00:00"
    assert row["severity"] == "error"


def test_distinct_issues_and_files_all_survive(tmp_path):
    rows = [
        ("/x/a.m4a", "NO_GENRE", "warning", "r1", "t"),
        ("/x/a.m4a", "NO_YEAR", "warning", "r1", "t"),
        ("/x/b.m4a", "NO_GENRE", "warning", "r1", "t"),
        ("/x/a.m4a", "NO_GENRE", "warning", "r2", "t"),  # the only duplicate
    ]
    conn = _legacy_db(tmp_path / "old.db", rows)
    assert _rebuild_validation_issues(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM validation_issues").fetchone()[0] == 3


def test_the_new_key_is_in_place_afterwards(tmp_path):
    conn = _legacy_db(tmp_path / "old.db", [])
    _rebuild_validation_issues(conn)
    assert not _validation_issues_key_includes_run_id(conn)


def test_the_rebuild_is_idempotent_through_open_db(tmp_path):
    """open_db applies it; opening twice must not fail or lose rows."""
    path = tmp_path / "old.db"
    conn = _legacy_db(path, [("/x/a.m4a", "NO_GENRE", "warning", "r1", "t")])
    conn.close()

    c1 = open_db(path)
    assert c1.execute("SELECT COUNT(*) FROM validation_issues").fetchone()[0] == 1
    c1.close()

    c2 = open_db(path)
    assert c2.execute("SELECT COUNT(*) FROM validation_issues").fetchone()[0] == 1
    assert not _validation_issues_key_includes_run_id(c2)
    c2.close()


# ── the behaviour the key is FOR ──────────────────────────────────────────────


def test_the_same_issue_seen_again_updates_rather_than_inserts(tmp_path):
    """The regrowth, reproduced against the real schema."""
    conn = open_db(tmp_path / "new.db")
    stmt = """
        INSERT INTO validation_issues (file_path, issue, severity, run_id, checked_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(file_path, issue) DO UPDATE SET
            run_id     = excluded.run_id,
            severity   = excluded.severity,
            checked_at = excluded.checked_at
    """
    for i in range(1, 21):
        conn.execute(stmt, ("/x/a.m4a", "NO_GENRE", "warning", f"run_{i}", f"t{i}"))
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM validation_issues").fetchone()[0] == 1
    row = conn.execute("SELECT run_id, checked_at FROM validation_issues").fetchone()
    assert row["run_id"] == "run_20", "last-seen must advance, not freeze"
    assert row["checked_at"] == "t20"
    conn.close()
