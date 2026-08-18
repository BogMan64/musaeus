"""
Tests for musaeus.db — schema, open_db(), log_event(), upsert_archive().
Uses a temporary in-memory SQLite DB (no files on disk).
"""

import sqlite3
from pathlib import Path

import pytest

from musaeus.db import (
    get_archive_by_status,
    get_archive_count,
    get_file_history,
    log_event,
    open_db,
    upsert_archive,
)


@pytest.fixture
def tmp_db(tmp_path: Path) -> sqlite3.Connection:
    """Fresh DB in a temp directory for each test."""
    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    yield conn
    conn.close()


# ── open_db / schema ──────────────────────────────────────────────────────────

class TestOpenDb:
    def test_creates_tables(self, tmp_db):
        tables = {
            row[0] for row in
            tmp_db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {"events", "archive", "duplicates", "validation_issues", "metadata_cache"} <= tables

    def test_wal_mode(self, tmp_db):
        mode = tmp_db.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_row_factory(self, tmp_db):
        # Row factory should give dict-like access
        row = tmp_db.execute("SELECT COUNT(*) AS cnt FROM archive").fetchone()
        assert row["cnt"] == 0

    def test_idempotent_open(self, tmp_path):
        """Opening the same DB twice should not corrupt schema."""
        db_path = tmp_path / "idempotent.db"
        conn1 = open_db(db_path)
        conn1.close()
        conn2 = open_db(db_path)
        count = conn2.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
        assert count == 0
        conn2.close()


# ── log_event() ───────────────────────────────────────────────────────────────

class TestLogEvent:
    def test_basic_event(self, tmp_db):
        log_event(tmp_db, "run_001", "RUN_START")
        tmp_db.commit()
        row = tmp_db.execute("SELECT * FROM events WHERE run_id='run_001'").fetchone()
        assert row["event_type"] == "RUN_START"

    def test_all_fields(self, tmp_db):
        log_event(
            tmp_db,
            run_id="run_002",
            event_type="INGEST",
            file_path="/vault/track.flac",
            old_value="old",
            new_value="new",
            stage="ingest",
            note="size=1234",
        )
        tmp_db.commit()
        row = tmp_db.execute(
            "SELECT * FROM events WHERE run_id='run_002'"
        ).fetchone()
        assert row["file_path"] == "/vault/track.flac"
        assert row["stage"] == "ingest"
        assert row["note"] == "size=1234"

    def test_multiple_events_same_run(self, tmp_db):
        for i in range(5):
            log_event(tmp_db, "run_multi", f"EVENT_{i}")
        tmp_db.commit()
        count = tmp_db.execute(
            "SELECT COUNT(*) FROM events WHERE run_id='run_multi'"
        ).fetchone()[0]
        assert count == 5

    def test_get_file_history(self, tmp_db):
        path = "/vault/test.mp3"
        for etype in ("INGEST", "HASH_COMPUTED", "METADATA_EXTRACTED"):
            log_event(tmp_db, "run_hist", etype, file_path=path)
        tmp_db.commit()
        history = get_file_history(tmp_db, path)
        assert len(history) == 3
        assert history[0]["event_type"] == "INGEST"
        assert history[-1]["event_type"] == "METADATA_EXTRACTED"


# ── upsert_archive() ──────────────────────────────────────────────────────────

class TestUpsertArchive:
    def test_insert_basic(self, tmp_db):
        upsert_archive(tmp_db, {
            "file_path": "/vault/track.flac",
            "filename": "track.flac",
            "ext": ".flac",
            "size_bytes": 50_000_000,
            "status": "PENDING",
        })
        tmp_db.commit()
        assert get_archive_count(tmp_db) == 1

    def test_upsert_updates_existing(self, tmp_db):
        path = "/vault/track.flac"
        upsert_archive(tmp_db, {"file_path": path, "status": "PENDING"})
        tmp_db.commit()
        upsert_archive(tmp_db, {"file_path": path, "status": "HASHED", "audio_hash": "abc123"})
        tmp_db.commit()
        row = tmp_db.execute("SELECT * FROM archive WHERE file_path=?", (path,)).fetchone()
        assert row["status"] == "HASHED"
        assert row["audio_hash"] == "abc123"
        assert get_archive_count(tmp_db) == 1  # no duplicate row

    def test_idempotent_insert(self, tmp_db):
        row = {"file_path": "/vault/x.mp3", "status": "PENDING"}
        upsert_archive(tmp_db, row)
        upsert_archive(tmp_db, row)
        tmp_db.commit()
        assert get_archive_count(tmp_db) == 1

    def test_get_by_status(self, tmp_db):
        upsert_archive(tmp_db, {"file_path": "/vault/a.flac", "status": "PENDING"})
        upsert_archive(tmp_db, {"file_path": "/vault/b.flac", "status": "HASHED"})
        upsert_archive(tmp_db, {"file_path": "/vault/c.flac", "status": "PENDING"})
        tmp_db.commit()
        pending = get_archive_by_status(tmp_db, "PENDING")
        assert len(pending) == 2

    def test_bitrate_stored_as_int(self, tmp_db):
        """Regression: bitrate must always be INTEGER, never a string."""
        upsert_archive(tmp_db, {
            "file_path": "/vault/t.mp3",
            "bitrate": 320000,
            "status": "CATALOGUED",
        })
        tmp_db.commit()
        row = tmp_db.execute(
            "SELECT bitrate FROM archive WHERE file_path='/vault/t.mp3'"
        ).fetchone()
        assert isinstance(row["bitrate"], int)
        assert row["bitrate"] == 320000


# ── validation_issues UNIQUE constraint ───────────────────────────────────────

class TestValidationIssues:
    def test_unique_constraint(self, tmp_db):
        """Same file+issue+run_id should not create duplicate rows."""
        for _ in range(3):
            tmp_db.execute(
                """
                INSERT OR IGNORE INTO validation_issues (file_path, issue, run_id)
                VALUES (?, ?, ?)
                """,
                ("/vault/bad.mp3", "missing_artist", "run_001"),
            )
        tmp_db.commit()
        count = tmp_db.execute(
            "SELECT COUNT(*) FROM validation_issues"
        ).fetchone()[0]
        assert count == 1

    def test_different_runs_allowed(self, tmp_db):
        """Same file+issue but different run_id → two rows (OK)."""
        for run in ("run_001", "run_002"):
            tmp_db.execute(
                """
                INSERT OR IGNORE INTO validation_issues (file_path, issue, run_id)
                VALUES (?, ?, ?)
                """,
                ("/vault/bad.mp3", "missing_artist", run),
            )
        tmp_db.commit()
        count = tmp_db.execute(
            "SELECT COUNT(*) FROM validation_issues"
        ).fetchone()[0]
        assert count == 2
