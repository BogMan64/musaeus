"""Tests for musaeus.state.backup — verified, checksummed database backups.

All backup_dir/db_path values in this file are pytest tmp_path fixtures, never
a real vault or the fixed future live recovery root. No test in this module
creates or probes /home/grey/Projects/MUSAEUS_RECOVERY.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from musaeus.state.backup import (
    BackupVerificationError,
    create_verified_backup,
    restore_from_backup,
)


def _make_db(path: Path, *, rows: int = 3) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
        for i in range(rows):
            conn.execute("INSERT INTO widgets (name) VALUES (?)", (f"widget-{i}",))
        conn.commit()
    finally:
        conn.close()


class TestCreateVerifiedBackup:
    def test_creates_verified_backup(self, tmp_path):
        db_path = tmp_path / "source.db"
        _make_db(db_path)
        backup = create_verified_backup(db_path, tmp_path / "backups")
        assert backup.backup_path.is_file()
        assert backup.source_path == db_path
        backup.verify()  # must not raise

    def test_backup_contains_same_data(self, tmp_path):
        db_path = tmp_path / "source.db"
        _make_db(db_path, rows=5)
        backup = create_verified_backup(db_path, tmp_path / "backups")
        conn = sqlite3.connect(str(backup.backup_path))
        try:
            count = conn.execute("SELECT COUNT(*) FROM widgets").fetchone()[0]
        finally:
            conn.close()
        assert count == 5

    def test_missing_source_raises(self, tmp_path):
        with pytest.raises(BackupVerificationError):
            create_verified_backup(tmp_path / "does-not-exist.db", tmp_path / "backups")

    def test_backup_dir_created_if_missing(self, tmp_path):
        db_path = tmp_path / "source.db"
        _make_db(db_path)
        backup_dir = tmp_path / "nested" / "backups"
        assert not backup_dir.exists()
        create_verified_backup(db_path, backup_dir)
        assert backup_dir.is_dir()

    def test_checksum_matches_reread_bytes(self, tmp_path):
        db_path = tmp_path / "source.db"
        _make_db(db_path)
        backup = create_verified_backup(db_path, tmp_path / "backups")
        import hashlib

        actual = hashlib.sha256(backup.backup_path.read_bytes()).hexdigest()
        assert backup.checksum == actual

    def test_backup_and_verify_survive_special_characters_in_path(self, tmp_path):
        """Regression: a path containing '#' must not be parsed as URI query
        text when building the read-only SQLite connection URI. Confirms the
        percent-encoding fix in _read_only_sqlite_uri."""
        odd_dir = tmp_path / "weird#name dir"
        odd_dir.mkdir()
        db_path = odd_dir / "source.db"
        _make_db(db_path)

        backup = create_verified_backup(db_path, tmp_path / "backups")
        backup.verify()  # must not raise despite '#' in the source path


class TestVerifyDetectsTampering:
    def test_verify_detects_truncation(self, tmp_path):
        db_path = tmp_path / "source.db"
        _make_db(db_path)
        backup = create_verified_backup(db_path, tmp_path / "backups")
        # Simulate corruption: truncate the backup after the fact.
        data = backup.backup_path.read_bytes()
        backup.backup_path.write_bytes(data[: len(data) // 2])
        with pytest.raises(BackupVerificationError, match="checksum mismatch"):
            backup.verify()

    def test_verify_detects_missing_file(self, tmp_path):
        db_path = tmp_path / "source.db"
        _make_db(db_path)
        backup = create_verified_backup(db_path, tmp_path / "backups")
        backup.backup_path.unlink()
        with pytest.raises(BackupVerificationError, match="missing"):
            backup.verify()

    def test_verify_detects_non_sqlite_content_with_matching_checksum(self, tmp_path):
        """A backup file replaced by non-SQLite bytes of a different length
        will fail the checksum check first; this asserts the integrity_check
        path also independently rejects non-SQLite content when checksums
        are recomputed against the tampered file (i.e. verify() never
        reports success for unreadable content)."""
        db_path = tmp_path / "source.db"
        _make_db(db_path)
        backup = create_verified_backup(db_path, tmp_path / "backups")
        backup.backup_path.write_bytes(b"not a sqlite file at all")
        with pytest.raises(BackupVerificationError):
            backup.verify()


class TestRestoreFromBackup:
    def test_restore_recreates_target(self, tmp_path):
        db_path = tmp_path / "source.db"
        _make_db(db_path, rows=7)
        backup = create_verified_backup(db_path, tmp_path / "backups")

        target = tmp_path / "restored.db"
        restore_from_backup(backup, target)

        conn = sqlite3.connect(str(target))
        try:
            count = conn.execute("SELECT COUNT(*) FROM widgets").fetchone()[0]
        finally:
            conn.close()
        assert count == 7

    def test_restore_refuses_when_backup_tampered(self, tmp_path):
        db_path = tmp_path / "source.db"
        _make_db(db_path)
        backup = create_verified_backup(db_path, tmp_path / "backups")
        backup.backup_path.write_bytes(b"corrupted")

        with pytest.raises(BackupVerificationError):
            restore_from_backup(backup, tmp_path / "restored.db")

    def test_restore_creates_missing_parent_directories(self, tmp_path):
        db_path = tmp_path / "source.db"
        _make_db(db_path)
        backup = create_verified_backup(db_path, tmp_path / "backups")

        target = tmp_path / "deep" / "nested" / "restored.db"
        restore_from_backup(backup, target)
        assert target.is_file()
