"""A column name from a hand-edited file must be checked before it becomes SQL.

P1-H, reported 2026-09-09.

`apply_approved_fixes` builds its statement as

    f"UPDATE archive SET {entry.field_name} = ? WHERE file_path = ?"

The *value* is bound. The *column* cannot be — SQLite has no parameter form
for an identifier — so it is interpolated. And `entry.field_name` is read
from a review TSV that a person edits by hand.

Unchecked, a typo produced a confusing `OperationalError` mid-run, and a
crafted value was arbitrary SQL against the library. This is the one place in
the codebase where user-supplied text reaches a statement uncontrolled;
`sanitize.py`'s f-string SQL was reviewed the same day and is safe, because
its interpolated names are module constants.

The allowlist is read from `PRAGMA table_info(archive)` rather than
hard-coded, because `ensure_columns()` adds columns lazily at runtime — ten
stages do it. A hard-coded list would reject a legitimate column the moment
a stage introduced one, and this project has a long record of guards that
drift out of step with the thing they guard.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from musaeus.approval import apply_approved_fixes
from musaeus.db import open_db


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = open_db(tmp_path / "musaeus.db")
    c.execute(
        "INSERT INTO archive (file_path, status, artist, title) "
        "VALUES ('/x/a.m4a', 'CATALOGUED', 'Old Artist', 'A Song')"
    )
    c.commit()
    return c


def _tsv(path: Path, field: str, value: str = "New Artist") -> Path:
    p = path / "review.tsv"
    p.write_text(
        "file_path\tfield_name\tcurrent_value\tsuggested_value\tsource\tapprove\n"
        f"/x/a.m4a\t{field}\tOld Artist\t{value}\ttest\tyes\n",
        encoding="utf-8",
    )
    return p


def test_a_real_column_is_applied(conn, tmp_path: Path) -> None:
    """The allowlist must not break the feature it protects."""
    report = apply_approved_fixes(conn, _tsv(tmp_path, "artist"), "run1")
    assert report.errors == []
    assert (
        conn.execute("SELECT artist FROM archive WHERE file_path='/x/a.m4a'").fetchone()[0]
        == "New Artist"
    )


def test_a_column_that_does_not_exist_is_refused(conn, tmp_path: Path) -> None:
    """A typo. Before the fix this raised OperationalError mid-run."""
    report = apply_approved_fixes(conn, _tsv(tmp_path, "artistt"), "run1")
    assert report.errors, "a non-column was accepted"
    assert "not a column" in report.errors[0]


def test_an_injection_attempt_is_refused_and_changes_nothing(conn, tmp_path: Path) -> None:
    """The reason this is P1 rather than a papercut.

    `artist = 'x' WHERE 1=1 --` would have rewritten every artist in the
    library from a single approved row.
    """
    field = "artist = 'pwned' WHERE 1=1 --"
    report = apply_approved_fixes(conn, _tsv(tmp_path, field), "run1")

    assert report.errors, "an injection payload was accepted"
    assert (
        conn.execute("SELECT artist FROM archive WHERE file_path='/x/a.m4a'").fetchone()[0]
        == "Old Artist"
    ), "the library was modified"


def test_a_refused_row_does_not_stop_the_others(conn, tmp_path: Path) -> None:
    """One bad line must not abandon the rest of the file."""
    p = tmp_path / "review.tsv"
    p.write_text(
        "file_path\tfield_name\tcurrent_value\tsuggested_value\tsource\tapprove\n"
        "/x/a.m4a\tnope\tOld Artist\tX\ttest\tyes\n"
        "/x/a.m4a\ttitle\tA Song\tA Better Song\ttest\tyes\n",
        encoding="utf-8",
    )
    report = apply_approved_fixes(conn, p, "run1")

    assert len(report.errors) == 1
    assert (
        conn.execute("SELECT title FROM archive WHERE file_path='/x/a.m4a'").fetchone()[0]
        == "A Better Song"
    )


def test_a_column_added_lazily_at_runtime_is_accepted(conn, tmp_path: Path) -> None:
    """Why the allowlist reads the schema instead of hard-coding names.

    ensure_columns() adds columns at runtime in ten stages. A frozen list
    would refuse a legitimate column the moment one of them ran.
    """
    # nosemgrep: alter-table-add-column-outside-db -- the point of the test is a column db.py did not create
    conn.execute("ALTER TABLE archive ADD COLUMN a_late_column TEXT")
    conn.commit()

    report = apply_approved_fixes(conn, _tsv(tmp_path, "a_late_column", "set"), "run1")
    assert report.errors == []


def test_a_dry_run_refuses_the_bad_row_without_writing_the_good_one(conn, tmp_path: Path) -> None:
    report = apply_approved_fixes(conn, _tsv(tmp_path, "artist"), "run1", dry_run=True)
    assert report.errors == []
    assert (
        conn.execute("SELECT artist FROM archive WHERE file_path='/x/a.m4a'").fetchone()[0]
        == "Old Artist"
    ), "a dry run wrote to the archive"
