"""Tests for musaeus.state.schema — version metadata, ledger, and registry."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from musaeus.state.schema import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    MIN_COMPATIBLE_SCHEMA_VERSION,
    UnsupportedSchemaVersionError,
    assert_supported_version,
    ensure_metadata_tables,
    list_migration_ledger,
    read_schema_version,
    stamp_current_version_if_unversioned,
)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(tmp_path / "state.db"))
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


class TestMetadataTables:
    def test_ensure_metadata_tables_idempotent(self, conn):
        ensure_metadata_tables(conn)
        ensure_metadata_tables(conn)  # must not raise on second call
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {"state_metadata", "schema_migrations"} <= tables

    def test_read_schema_version_none_when_unstamped(self, conn):
        ensure_metadata_tables(conn)
        assert read_schema_version(conn) is None

    def test_stamp_current_version_if_unversioned(self, conn):
        ensure_metadata_tables(conn)
        stamp_current_version_if_unversioned(conn, 2, 0)
        assert read_schema_version(conn) == 2

    def test_stamp_does_not_overwrite_existing_row(self, conn):
        ensure_metadata_tables(conn)
        stamp_current_version_if_unversioned(conn, 2, 0)
        stamp_current_version_if_unversioned(conn, 99, 0)  # must be a no-op
        assert read_schema_version(conn) == 2


class TestAssertSupportedVersion:
    def test_current_version_is_supported(self):
        assert_supported_version(CURRENT_SCHEMA_VERSION)

    def test_min_compatible_version_is_supported(self):
        assert_supported_version(MIN_COMPATIBLE_SCHEMA_VERSION)

    def test_future_version_rejected(self):
        with pytest.raises(UnsupportedSchemaVersionError, match="newer"):
            assert_supported_version(CURRENT_SCHEMA_VERSION + 1)

    def test_below_minimum_rejected(self):
        with pytest.raises(UnsupportedSchemaVersionError, match="older"):
            assert_supported_version(MIN_COMPATIBLE_SCHEMA_VERSION - 1)


class TestMigrationRegistry:
    def test_registry_is_contiguous_from_zero(self):
        """The registry must form one unbroken chain ending at CURRENT_SCHEMA_VERSION."""
        by_from = {m.from_version: m for m in MIGRATIONS}
        version = 0
        seen = 0
        while version in by_from:
            assert seen < len(MIGRATIONS), "Migration registry contains a cycle"
            version = by_from[version].to_version
            seen += 1
        assert seen == len(MIGRATIONS)
        assert version == CURRENT_SCHEMA_VERSION

    def test_migration_ids_are_unique(self):
        ids = [m.migration_id for m in MIGRATIONS]
        assert len(ids) == len(set(ids))

    def test_list_migration_ledger_empty_initially(self, conn):
        ensure_metadata_tables(conn)
        assert list_migration_ledger(conn) == []
