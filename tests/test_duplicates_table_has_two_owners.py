"""The `duplicates` table is claimed by two subsystems, and only one of
them is running.

WHAT IS NOT WRONG
Migration 0003 is careful and does the right thing. It does NOT clobber
the live table: it renames it to `duplicates_legacy`, creates the typed
DR-07 table in its place, and carries every legacy row across into a
documented compatibility payload. Nothing is dropped and nothing is lost.

WHAT IS STILL TRUE
The live dedupe subsystem -- musaeus/dedupe.py, stages/dupe_resolver.py,
stages/cross_dupe.py, cli.py and two scripts -- queries `duplicates` for
`group_id`, `file_path` and `status`. After migration 0003 those columns
are on `duplicates_legacy`, and `duplicates` is the typed table. So the
migration and the live code cannot both be right about what `duplicates`
means, and applying 0003 to the real vault would stop the dupe resolver
working -- with the data intact but out from under it.

That is not hypothetical arithmetic: on 2026-09-09 the live database held
118,395 legacy rows and 2,862 pending groups, and had never had migration
0001 applied (no `state_metadata` table), so the whole chain is unrun.

This test pins the coupling so it cannot be discovered by running it. It
passes today by asserting the incompatibility. It fails the day someone
ports the live code to the typed table (delete it then -- the coupling is
gone) or changes the migration (fix whichever side moved). Either way the
change becomes a decision instead of a surprise.
"""

from __future__ import annotations

import sqlite3

import pytest

from musaeus.db import open_db
from musaeus.state.migrator import migrate


def _migrate(db_path) -> None:
    """migrate() requires an explicit recovery root -- it will not pick one."""
    recovery = db_path.parent / "recovery"
    recovery.mkdir(exist_ok=True)
    migrate(db_path, recovery_root=recovery)


# One query per live module that reads the table, copied verbatim enough to
# fail for the same reason the real one would.
LIVE_QUERIES = {
    "stages/dupe_resolver.py": "SELECT file_path, group_id FROM duplicates",
    "cli.py": "SELECT COUNT(DISTINCT group_id) FROM duplicates WHERE status='pending'",
    "stages/cross_dupe.py": "SELECT 1 FROM duplicates WHERE file_path = 'x'",
    "dedupe.py": "UPDATE duplicates SET status = 'keep' WHERE group_id = 'g' AND file_path = 'p'",
}


@pytest.fixture
def legacy_db(tmp_path):
    path = tmp_path / "musaeus.db"
    conn = open_db(path)
    conn.execute(
        "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, "
        "status, run_id, staged_at) VALUES (?,?,?,?,?,?,?)",
        ("grp-1", "/x/a.m4a", "EXACT", 1.0, "pending", "run-1", "2026-09-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    return path


def test_the_live_code_works_before_the_migration(legacy_db) -> None:
    conn = sqlite3.connect(legacy_db)
    try:
        for who, sql in LIVE_QUERIES.items():
            try:
                conn.execute(sql)
            except sqlite3.OperationalError as exc:  # pragma: no cover - guard
                pytest.fail(f"{who} cannot run against the shipped schema: {exc}")
    finally:
        conn.close()


def test_every_live_query_breaks_after_migration_0003(legacy_db) -> None:
    """Named one by one rather than as a count, so the failure message says
    which subsystem stops working."""
    _migrate(legacy_db)
    conn = sqlite3.connect(legacy_db)
    try:
        for who, sql in LIVE_QUERIES.items():
            with pytest.raises(sqlite3.OperationalError, match="no such column"):
                conn.execute(sql)
                pytest.fail(f"{who} unexpectedly still works — port it or drop this test")
    finally:
        conn.close()


def test_the_migration_keeps_every_legacy_row(legacy_db) -> None:
    """The reassuring half, asserted so it stays true: renamed, not dropped,
    and carried across."""
    _migrate(legacy_db)
    conn = sqlite3.connect(legacy_db)
    try:
        kept = conn.execute("SELECT group_id, file_path, status FROM duplicates_legacy").fetchall()
        assert kept == [("grp-1", "/x/a.m4a", "pending")]
        carried = conn.execute(
            "SELECT COUNT(*) FROM duplicates WHERE detector LIKE 'legacy%'"
        ).fetchone()[0]
        assert carried == 1
    finally:
        conn.close()
