"""Tests for musaeus.state.migrate — the P0-06 acceptance-gate suite.

Covers (see .kiro/specs/musaeus-consumer-readiness/tasks.md, P0-06):
- Migrating a fresh, empty database stamps it at the current version with no
  backup and no migration ledger entries (nothing to migrate from).
- Migrating a legacy database (pre-existing tables, no state_metadata row) is
  detected as version 0 and walks the full registered migration path.
- An injected migration failure rolls back cleanly and leaves the prior
  database state usable; a 'failed' ledger row records the failure.
- An unsupported (future) recorded version is refused before any mutation.
- The candidate-swap path validates the candidate before ever touching the
  live file, and leaves the live file untouched on candidate failure.

Every db_path/backup_dir/work_dir here is a pytest tmp_path fixture. No test
touches a real vault or the fixed future live recovery root
(/home/grey/Projects/MUSAEUS_RECOVERY) — this module never even imports a
path constant for it, by design.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from musaeus.state.backup import BackupVerificationError
from musaeus.state.migrate import (
    migrate_database,
    migrate_via_candidate_swap,
)
from musaeus.state.schema import (
    CURRENT_SCHEMA_VERSION,
    Migration,
    MigrationFailedError,
    UnsupportedSchemaVersionError,
    read_schema_version,
)


def _legacy_db(path: Path) -> None:
    """A pre-versioning database: has real content, no state_metadata row."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE archive (id INTEGER PRIMARY KEY, file_path TEXT UNIQUE NOT NULL)"
        )
        conn.execute("INSERT INTO archive (file_path) VALUES ('/vault/a.flac')")
        conn.commit()
    finally:
        conn.close()


class TestFreshDatabase:
    def test_fresh_empty_database_is_stamped_with_no_backup(self, tmp_path):
        db_path = tmp_path / "fresh.db"
        conn = sqlite3.connect(str(db_path))
        conn.close()

        outcome = migrate_database(db_path, tmp_path / "backups")

        assert outcome.already_current is True
        assert outcome.backup is None
        assert outcome.applied == ()
        assert outcome.to_version == CURRENT_SCHEMA_VERSION
        assert not (tmp_path / "backups").exists()

    def test_fresh_database_second_call_is_still_noop(self, tmp_path):
        db_path = tmp_path / "fresh.db"
        sqlite3.connect(str(db_path)).close()
        migrate_database(db_path, tmp_path / "backups")

        outcome = migrate_database(db_path, tmp_path / "backups")
        assert outcome.already_current is True
        assert outcome.applied == ()


class TestLegacyDatabaseMigration:
    def test_legacy_database_migrates_to_current_with_verified_backup(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)

        outcome = migrate_database(db_path, tmp_path / "backups")

        assert outcome.from_version == 0
        assert outcome.to_version == CURRENT_SCHEMA_VERSION
        assert outcome.already_current is False
        assert outcome.backup is not None
        outcome.backup.verify()  # backup must be independently verifiable
        assert len(outcome.applied) > 0

    def test_migration_ledger_records_every_applied_migration(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)
        outcome = migrate_database(db_path, tmp_path / "backups")

        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT migration_id, outcome FROM schema_migrations ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        assert [r[0] for r in rows] == list(outcome.applied)
        assert all(r[1] == "succeeded" for r in rows)

    def test_legacy_data_is_preserved_through_migration(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)
        migrate_database(db_path, tmp_path / "backups")

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT file_path FROM archive WHERE file_path='/vault/a.flac'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None

    def test_schema_version_recorded_after_migration(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)
        migrate_database(db_path, tmp_path / "backups")

        conn = sqlite3.connect(str(db_path))
        try:
            assert read_schema_version(conn) == CURRENT_SCHEMA_VERSION
        finally:
            conn.close()

    def test_second_migration_call_is_noop(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)
        migrate_database(db_path, tmp_path / "backups")

        outcome = migrate_database(db_path, tmp_path / "backups")
        assert outcome.already_current is True
        assert outcome.applied == ()


class TestMigrationFailureRollsBack:
    def test_failed_migration_leaves_prior_state_intact(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)

        def _boom(conn: sqlite3.Connection) -> None:
            raise RuntimeError("simulated migration failure")

        broken_migration = Migration(
            migration_id="9999_simulated_failure",
            from_version=0,
            to_version=CURRENT_SCHEMA_VERSION,
            description="Deliberately broken migration for rollback test.",
            apply=_boom,
        )
        with (
            patch("musaeus.state.migrate.MIGRATIONS", (broken_migration,)),
            pytest.raises(MigrationFailedError, match="simulated migration failure"),
        ):
            migrate_database(db_path, tmp_path / "backups")

        # Prior state must remain: the archive table and its row still exist,
        # and no partial schema_version was committed.
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT file_path FROM archive WHERE file_path='/vault/a.flac'"
            ).fetchone()
            assert row is not None
            assert read_schema_version(conn) is None
        finally:
            conn.close()

    def test_failed_migration_records_failed_ledger_row(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)

        def _boom(conn: sqlite3.Connection) -> None:
            raise RuntimeError("simulated migration failure")

        broken_migration = Migration(
            migration_id="9999_simulated_failure",
            from_version=0,
            to_version=CURRENT_SCHEMA_VERSION,
            description="Deliberately broken migration for rollback test.",
            apply=_boom,
        )
        with (
            patch("musaeus.state.migrate.MIGRATIONS", (broken_migration,)),
            pytest.raises(MigrationFailedError),
        ):
            migrate_database(db_path, tmp_path / "backups")

        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT migration_id, outcome, error_message FROM schema_migrations"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "9999_simulated_failure"
        assert rows[0][1] == "failed"
        assert "simulated migration failure" in rows[0][2]

    def test_backup_taken_before_failed_migration_remains_verifiable(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)

        def _boom(conn: sqlite3.Connection) -> None:
            raise RuntimeError("simulated migration failure")

        broken_migration = Migration(
            migration_id="9999_simulated_failure",
            from_version=0,
            to_version=CURRENT_SCHEMA_VERSION,
            description="Deliberately broken migration for rollback test.",
            apply=_boom,
        )
        backup_dir = tmp_path / "backups"
        with (
            patch("musaeus.state.migrate.MIGRATIONS", (broken_migration,)),
            pytest.raises(MigrationFailedError),
        ):
            migrate_database(db_path, backup_dir)

        backups = list(backup_dir.glob("*.backup.db"))
        assert len(backups) == 1  # the pre-migration backup survives the failure


class TestUnsupportedVersionBlocked:
    def test_future_schema_version_is_refused(self, tmp_path):
        db_path = tmp_path / "future.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE state_metadata (id INTEGER PRIMARY KEY CHECK (id=1), "
            "schema_version INTEGER NOT NULL, min_compatible_version INTEGER NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO state_metadata VALUES (1, ?, 0, 'now', 'now')",
            (CURRENT_SCHEMA_VERSION + 5,),
        )
        conn.commit()
        conn.close()

        with pytest.raises(UnsupportedSchemaVersionError, match="newer"):
            migrate_database(db_path, tmp_path / "backups")

        # No backup was taken — we refused before touching anything.
        assert not (tmp_path / "backups").exists()


class TestCandidateSwapMigration:
    def test_candidate_swap_migrates_legacy_database(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)

        result = migrate_via_candidate_swap(db_path, tmp_path / "backups", tmp_path / "work")

        assert result.to_version == CURRENT_SCHEMA_VERSION
        assert result.backup is not None
        result.backup.verify()

        conn = sqlite3.connect(str(db_path))
        try:
            assert read_schema_version(conn) == CURRENT_SCHEMA_VERSION
            row = conn.execute(
                "SELECT file_path FROM archive WHERE file_path='/vault/a.flac'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_candidate_swap_leaves_live_file_untouched_on_failure(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)

        def _boom(conn: sqlite3.Connection) -> None:
            raise RuntimeError("candidate migration failure")

        broken_migration = Migration(
            migration_id="9999_simulated_failure",
            from_version=0,
            to_version=CURRENT_SCHEMA_VERSION,
            description="Deliberately broken migration for candidate-swap test.",
            apply=_boom,
        )
        work_dir = tmp_path / "work"
        with (
            patch("musaeus.state.migrate.MIGRATIONS", (broken_migration,)),
            pytest.raises(MigrationFailedError),
        ):
            migrate_via_candidate_swap(db_path, tmp_path / "backups", work_dir)

        # The live file's *logical* content is untouched: still unversioned
        # (never stamped), still holding the original legacy row, and it is
        # never replaced by the (failed) candidate. Raw bytes are not
        # compared here because the pre-copy WAL checkpoint on db_path is a
        # legitimate on-disk layout change (WAL merge into the main file)
        # that happens before the candidate is even created, independent of
        # whether the migration itself later fails.
        conn = sqlite3.connect(str(db_path))
        try:
            assert read_schema_version(conn) is None
            row = conn.execute(
                "SELECT file_path FROM archive WHERE file_path='/vault/a.flac'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()
        # No leftover candidate file remains in the work directory.
        leftover = list(work_dir.glob("*.candidate.*"))
        assert leftover == []

    def test_candidate_swap_noop_for_fresh_database(self, tmp_path):
        db_path = tmp_path / "fresh.db"
        sqlite3.connect(str(db_path)).close()

        result = migrate_via_candidate_swap(db_path, tmp_path / "backups", tmp_path / "work")
        assert result.already_current is True
        assert result.backup is None


class TestCandidateSwapCleansUpSidecarsOnSuccess:
    def test_no_leftover_candidate_files_after_successful_swap(self, tmp_path):
        """Regression: WAL/SHM sidecars for the candidate's temporary name
        must not survive a *successful* swap. Only the except-branch cleanup
        existed before; the work_dir must end up empty after success too."""
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)
        work_dir = tmp_path / "work"

        migrate_via_candidate_swap(db_path, tmp_path / "backups", work_dir)

        leftover = list(work_dir.glob("*.candidate.*"))
        assert leftover == [], f"candidate/sidecar files leaked into work_dir: {leftover}"


class TestBackwardsMigrationRefused:
    def test_cannot_migrate_backwards(self, tmp_path):
        db_path = tmp_path / "ahead.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE state_metadata (id INTEGER PRIMARY KEY CHECK (id=1), "
            "schema_version INTEGER NOT NULL, min_compatible_version INTEGER NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO state_metadata VALUES (1, ?, 0, 'now', 'now')",
            (CURRENT_SCHEMA_VERSION,),
        )
        conn.commit()
        conn.close()

        with pytest.raises(MigrationFailedError, match="backwards"):
            migrate_database(db_path, tmp_path / "backups", target_version=0)


class TestLegacyDatabaseWithoutArchiveTable:
    def test_legacy_database_without_archive_table_migrates_cleanly(self, tmp_path):
        """Regression: a version-0 database whose only content is unrelated
        to 'archive' (e.g. just the legacy duplicates table) must not crash
        migration 0001 with 'no such table: archive'."""
        db_path = tmp_path / "legacy_no_archive.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("CREATE TABLE duplicates (id INTEGER PRIMARY KEY, file_path TEXT)")
            conn.execute("INSERT INTO duplicates (file_path) VALUES ('/vault/dupe.mp3')")
            conn.commit()
        finally:
            conn.close()

        outcome = migrate_database(db_path, tmp_path / "backups")

        assert outcome.to_version == CURRENT_SCHEMA_VERSION
        conn = sqlite3.connect(str(db_path))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            # The legacy table was renamed by migration 0002, not lost.
            assert "duplicates_legacy_v1" in tables
            assert "archive" not in tables  # migration 0001 was correctly a no-op
        finally:
            conn.close()


class TestDuplicatesNullFingerprintDeduplication:
    def test_null_fingerprint_candidates_deduplicate(self, tmp_path):
        """Regression: DR-07 dedup must hold even when a detector reports no
        fingerprint. fingerprint_digest is NOT NULL DEFAULT '' specifically
        so SQLite's UNIQUE index treats two no-fingerprint rows for the same
        candidate pair + detector as the same row, not as distinct NULLs.

        Uses a legacy database (not a bare empty file) so migration 0002
        actually runs and creates the canonical `duplicates` table — a
        genuinely empty database has nothing to migrate from and is simply
        stamped at the current version with no migrations applied.
        """
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)
        migrate_database(db_path, tmp_path / "backups")

        conn = sqlite3.connect(str(db_path))
        try:
            insert_sql = (
                "INSERT OR IGNORE INTO duplicates "
                "(run_id, candidate_item_id, matched_item_id, detector, evidence_json, created_at) "
                "VALUES ('run1', 'itemA', 'itemB', 'acoustid', '{}', 'now')"
            )
            conn.execute(insert_sql)
            conn.execute(insert_sql)
            conn.commit()
            count = conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0]
        finally:
            conn.close()
        assert count == 1


class TestMigrationLedgerRetryHistory:
    def test_retrying_after_failure_does_not_overwrite_failed_ledger_row(self, tmp_path):
        """Regression: the success UPDATE must target the specific attempt's
        row id, not migration_id — otherwise a retried migration_id's success
        silently rewrites an earlier 'failed' row for the same migration_id,
        erasing the audit trail of the original failure."""
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)

        call_count = {"n": 0}

        def _fail_once_then_succeed(conn: sqlite3.Connection) -> None:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("first attempt fails")
            conn.execute("CREATE TABLE IF NOT EXISTS marker (id INTEGER PRIMARY KEY)")

        flaky_migration = Migration(
            migration_id="9999_flaky",
            from_version=0,
            to_version=CURRENT_SCHEMA_VERSION,
            description="Fails on first attempt, succeeds on retry.",
            apply=_fail_once_then_succeed,
        )
        backup_dir = tmp_path / "backups"
        with (
            patch("musaeus.state.migrate.MIGRATIONS", (flaky_migration,)),
            pytest.raises(MigrationFailedError),
        ):
            migrate_database(db_path, backup_dir)

        # Retry: same migration_id, this time it succeeds.
        with patch("musaeus.state.migrate.MIGRATIONS", (flaky_migration,)):
            migrate_database(db_path, backup_dir)

        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT outcome FROM schema_migrations WHERE migration_id='9999_flaky' ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        # Two distinct ledger rows must exist: the original failure is
        # preserved, and the retry's success is a separate row.
        assert [r[0] for r in rows] == ["failed", "succeeded"]


class TestBackupVerificationErrorPropagates:
    def test_backup_failure_propagates_before_any_migration_attempt(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _legacy_db(db_path)

        with (
            patch(
                "musaeus.state.migrate.create_verified_backup",
                side_effect=BackupVerificationError("simulated backup failure"),
            ),
            pytest.raises(BackupVerificationError),
        ):
            migrate_database(db_path, tmp_path / "backups")

        # No migration ledger row should exist — we failed before applying anything.
        conn = sqlite3.connect(str(db_path))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "schema_migrations" in tables:
                count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
                assert count == 0
        finally:
            conn.close()
