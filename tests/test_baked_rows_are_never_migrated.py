"""A baked copy must never be migrated into the masters tier.

2026-09-05, done for real. ALAC_Archive is the pristine, never-baked tier;
ALAC-Library holds the -18 LUFS edition. After the bake, a row's file_path
points at its OUTPUT in ALAC-Library, not at its master.

migrate_to_archive selected on `status='CATALOGUED' AND finalized_at IS NOT
NULL AND file_path LIKE <ALAC-Library>%` with no notion of baking, so it
moved 1,765 normalised files into the masters tier -- 1,277 of them renamed
" (2).m4a" beside the very masters they were made from. Reversed from a
pre-migration snapshot with nothing lost, but the tier split is the entire
point of the two-directory design.

The bake's own _unmigrated_count() shared that selection deliberately ("so
the two scripts agree"), so it counted the same rows and printed advice
telling the operator to run the migration on them. The advice caused the
fault; both queries now exclude baked rows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _schema(conn):
    conn.execute(
        "CREATE TABLE archive (id INTEGER PRIMARY KEY, file_path TEXT UNIQUE, "
        "status TEXT, finalized_at TEXT, lufs_baked_at TEXT)"
    )


def _rows(conn, library: str):
    conn.executemany(
        "INSERT INTO archive (file_path, status, finalized_at, lufs_baked_at) VALUES (?,?,?,?)",
        [
            (f"{library}/a/master_never_baked.m4a", "CATALOGUED", "2026-01-01", None),
            (f"{library}/a/blank_baked_marker.m4a", "CATALOGUED", "2026-01-01", ""),
            (f"{library}/a/already_baked.m4a", "CATALOGUED", "2026-01-01", "2026-09-05"),
            (f"{library}/a/not_finalized.m4a", "CATALOGUED", None, None),
        ],
    )
    conn.commit()


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _schema(c)
    return c


LIB = "/vault/Libraries/ALAC-Library"


def _candidates(conn):
    """The query both scripts now share."""
    return [
        r["file_path"]
        for r in conn.execute(
            """
            SELECT file_path FROM archive
             WHERE status = 'CATALOGUED'
               AND finalized_at IS NOT NULL
               AND file_path LIKE ? || '%'
               AND (lufs_baked_at IS NULL OR lufs_baked_at = '')
            """,
            (LIB,),
        )
    ]


class TestSelection:
    def test_an_already_baked_row_is_not_a_migration_candidate(self, conn):
        _rows(conn, LIB)
        assert f"{LIB}/a/already_baked.m4a" not in _candidates(conn)

    def test_an_unbaked_master_still_is(self, conn):
        _rows(conn, LIB)
        assert f"{LIB}/a/master_never_baked.m4a" in _candidates(conn)

    def test_an_empty_string_counts_as_unbaked(self, conn):
        """lufs_baked_at = '' is 'not baked', not 'baked at no time'."""
        _rows(conn, LIB)
        assert f"{LIB}/a/blank_baked_marker.m4a" in _candidates(conn)

    def test_an_unfinalized_row_is_still_excluded(self, conn):
        _rows(conn, LIB)
        assert f"{LIB}/a/not_finalized.m4a" not in _candidates(conn)


class TestBothScriptsAgree:
    """The bake's warning and the migrate's selection must stay in step --
    they were deliberately written to share a shape, which is how one bug
    became two."""

    @pytest.mark.parametrize("path", [
        "scripts/musaeus_migrate_to_archive.py",
        "scripts/alac_library/build_alac_library.py",
    ])
    def test_the_baked_exclusion_is_present(self, path):
        src = (_ROOT / path).read_text(encoding="utf-8")
        assert "lufs_baked_at IS NULL OR lufs_baked_at = ''" in src, (
            f"{path} can select an already-baked row as unmigrated"
        )
