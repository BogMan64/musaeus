"""promote() must hand back a table that is still `archive` in every respect.

The rebuild table was built with `CREATE TABLE ... AS SELECT * FROM archive
WHERE 0`, which copies column names and types and NOTHING else: no PRIMARY
KEY, no AUTOINCREMENT, no UNIQUE(file_path), no NOT NULL, no DEFAULTs.
promote() then renamed that constraint-less table over `archive`.

Observed on the live vault 2026-08-30: every subsequent write died with
"ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint",
so ingest and sentinel both failed on their first row.

And SQLite carries a table's INDEXES along with ALTER TABLE RENAME, so the
named indexes followed the OLD table to its backup name -- leaving the
promoted table unindexed and the names taken, which makes db.py's
`CREATE INDEX IF NOT EXISTS` silently no-op for ever after. Measured on the
same DB: 1 index on archive, 4 on the backup.
"""

from __future__ import annotations

import sqlite3

import pytest

from musaeus.db import open_db, upsert_archive
from musaeus.rebuild_from_disk import create_rebuild_table, promote


def _make_rebuild_table(conn: sqlite3.Connection, table: str = "archive_rebuilt") -> None:
    """Build the shadow table using PRODUCTION code, not a copy of it.

    An earlier draft reimplemented the DDL here, so reverting the production
    function to `CREATE TABLE ... AS SELECT` left every test passing -- the
    test could not see the code it was meant to be testing.
    """
    create_rebuild_table(conn, table)
    conn.commit()


@pytest.fixture
def db(tmp_path):
    conn = open_db(tmp_path / "musaeus.db")
    upsert_archive(conn, {"file_path": "/x/a.m4a", "status": "CATALOGUED"})
    conn.commit()
    return conn


def _schema_of(conn, name):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row[0] if row else ""


def _indexes_of(conn, name):
    return sorted(
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? "
            "AND sql IS NOT NULL", (name,)
        )
    )


def test_the_promoted_table_keeps_the_constraints(db):
    before = _schema_of(db, "archive")
    assert "UNIQUE" in before and "AUTOINCREMENT" in before, "fixture is wrong"

    _make_rebuild_table(db)
    promote(db)

    after = _schema_of(db, "archive")
    assert "UNIQUE" in after, "UNIQUE(file_path) lost -- ON CONFLICT will fail"
    assert "AUTOINCREMENT" in after, "id PRIMARY KEY AUTOINCREMENT lost"
    assert "NOT NULL" in after, "NOT NULL lost"


def test_upserts_still_work_after_promotion(db):
    """The failure that actually stopped the pipeline."""
    _make_rebuild_table(db)
    promote(db)

    upsert_archive(db, {"file_path": "/x/b.m4a", "status": "PENDING", "artist": "One"})
    upsert_archive(db, {"file_path": "/x/b.m4a", "status": "PENDING", "artist": "Two"})
    db.commit()
    rows = db.execute("SELECT artist FROM archive WHERE file_path='/x/b.m4a'").fetchall()
    assert len(rows) == 1, "duplicate file_path rows became insertable"
    assert rows[0][0] == "Two", "ON CONFLICT did not update"


def test_the_named_indexes_survive_promotion(db):
    before = _indexes_of(db, "archive")
    assert before, "fixture is wrong -- open_db should have created indexes"

    _make_rebuild_table(db)
    backup = promote(db)

    assert _indexes_of(db, "archive") == before, "indexes did not follow the promotion"
    assert _indexes_of(db, backup) == [], "indexes were left on the backup table"


def test_the_old_table_is_preserved_not_dropped(db):
    _make_rebuild_table(db)
    backup = promote(db)
    n = db.execute(f"SELECT COUNT(*) FROM {backup}").fetchone()[0]
    assert n == 1, "the previous archive must be kept, never dropped"
