"""
Tests for musaeus.rebuild — rebuild_archive_from_events().

Verifies that the event log can reconstruct the archive table.
"""

import json
from pathlib import Path

import pytest

from musaeus.db import get_archive_count, log_event, open_db, upsert_archive
from musaeus.rebuild import rebuild_archive_from_events


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "rebuild_test.db"
    c = open_db(db_path)
    yield c
    c.close()


# ── Basic rebuild ─────────────────────────────────────────────────────────────

class TestRebuildBasic:
    def test_empty_db_rebuild(self, conn):
        """Rebuilding an empty DB produces empty archive."""
        summary = rebuild_archive_from_events(conn)
        assert summary["cleared"] == 0
        assert summary["replayed"] == 0
        assert summary["files_rebuilt"] == 0
        assert summary["errors"] == []

    def test_rebuild_from_ingest_events(self, conn):
        """FILE_REGISTERED events create archive entries."""
        # Simulate ingest events
        log_event(conn, "run_001", "FILE_REGISTERED", file_path="/vault/a.flac")
        log_event(conn, "run_001", "FILE_REGISTERED", file_path="/vault/b.mp3")
        conn.commit()

        summary = rebuild_archive_from_events(conn)
        assert summary["files_rebuilt"] == 2
        assert summary["errors"] == []

        # Verify archive was populated
        count = get_archive_count(conn)
        assert count == 2

    def test_rebuild_preserves_hashed_status(self, conn):
        """FILE_HASHED events set status to HASHED and store hashes."""
        log_event(conn, "run_001", "FILE_REGISTERED", file_path="/vault/x.flac")
        log_event(
            conn, "run_001", "FILE_HASHED",
            file_path="/vault/x.flac",
            new_value=json.dumps({"audio_hash": "abc123", "full_hash": "def456"}),
        )
        conn.commit()

        rebuild_archive_from_events(conn)
        row = conn.execute(
            "SELECT status, audio_hash, full_hash FROM archive WHERE file_path=?",
            ("/vault/x.flac",),
        ).fetchone()
        assert row["status"] == "HASHED"
        assert row["audio_hash"] == "abc123"
        assert row["full_hash"] == "def456"

    def test_rebuild_catalogued_status(self, conn):
        """FILE_CATALOGUED events bring metadata and status."""
        log_event(conn, "run_001", "FILE_REGISTERED", file_path="/vault/song.flac")
        meta = {"artist": "The Beatles", "title": "Yesterday", "bitrate": 320000}
        log_event(
            conn, "run_001", "FILE_CATALOGUED",
            file_path="/vault/song.flac",
            new_value=json.dumps(meta),
        )
        conn.commit()

        rebuild_archive_from_events(conn)
        row = conn.execute(
            "SELECT status, artist, title, bitrate FROM archive WHERE file_path=?",
            ("/vault/song.flac",),
        ).fetchone()
        assert row["status"] == "CATALOGUED"
        assert row["artist"] == "The Beatles"
        assert row["title"] == "Yesterday"
        assert row["bitrate"] == 320000


# ── Advanced replay scenarios ─────────────────────────────────────────────────

class TestRebuildAdvanced:
    def test_ghost_status(self, conn):
        """FILE_GHOST event marks the file as GHOST."""
        log_event(conn, "run_001", "FILE_REGISTERED", file_path="/vault/missing.flac")
        log_event(conn, "run_002", "FILE_GHOST", file_path="/vault/missing.flac")
        conn.commit()

        rebuild_archive_from_events(conn)
        row = conn.execute(
            "SELECT status FROM archive WHERE file_path=?",
            ("/vault/missing.flac",),
        ).fetchone()
        assert row["status"] == "GHOST"

    def test_removed_file_excluded(self, conn):
        """FILE_REMOVED means the file should NOT appear in rebuilt archive."""
        log_event(conn, "run_001", "FILE_REGISTERED", file_path="/vault/deleted.flac")
        log_event(conn, "run_002", "FILE_REMOVED", file_path="/vault/deleted.flac")
        conn.commit()

        rebuild_archive_from_events(conn)
        count = get_archive_count(conn)
        assert count == 0

    def test_status_change_event(self, conn):
        """STATUS_CHANGE event updates status field."""
        log_event(conn, "run_001", "FILE_REGISTERED", file_path="/vault/x.flac")
        log_event(conn, "run_001", "STATUS_CHANGE",
                  file_path="/vault/x.flac", new_value="CATALOGUED")
        conn.commit()

        rebuild_archive_from_events(conn)
        row = conn.execute(
            "SELECT status FROM archive WHERE file_path=?",
            ("/vault/x.flac",),
        ).fetchone()
        assert row["status"] == "CATALOGUED"

    def test_clears_existing_archive_before_rebuild(self, conn):
        """Rebuild clears the archive table first, then rebuilds."""
        # Pre-populate archive with a manual entry
        upsert_archive(conn, {"file_path": "/vault/pre_existing.flac", "status": "HASHED"})
        conn.commit()
        assert get_archive_count(conn) == 1

        # No events → rebuilt archive is empty
        summary = rebuild_archive_from_events(conn)
        assert summary["cleared"] == 1
        assert get_archive_count(conn) == 0

    def test_multiple_events_same_file(self, conn):
        """Multiple events for same file produce single archive entry."""
        log_event(conn, "run_001", "FILE_REGISTERED", file_path="/vault/track.flac")
        log_event(conn, "run_001", "FILE_HASHED", file_path="/vault/track.flac",
                  new_value=json.dumps({"audio_hash": "h1"}))
        log_event(conn, "run_001", "FILE_CATALOGUED", file_path="/vault/track.flac",
                  new_value=json.dumps({"artist": "A", "title": "T"}))
        conn.commit()

        rebuild_archive_from_events(conn)
        assert get_archive_count(conn) == 1
        row = conn.execute("SELECT * FROM archive WHERE file_path=?",
                          ("/vault/track.flac",)).fetchone()
        assert row["audio_hash"] == "h1"
        assert row["artist"] == "A"
        assert row["status"] == "CATALOGUED"


# ── Dry run mode ──────────────────────────────────────────────────────────────

class TestRebuildDryRun:
    def test_dry_run_no_changes(self, conn):
        """Dry run reports what WOULD happen without changing DB."""
        log_event(conn, "run_001", "FILE_REGISTERED", file_path="/vault/a.flac")
        log_event(conn, "run_001", "FILE_REGISTERED", file_path="/vault/b.flac")
        conn.commit()

        # Pre-populate archive
        upsert_archive(conn, {"file_path": "/vault/old.flac", "status": "PENDING"})
        conn.commit()

        summary = rebuild_archive_from_events(conn, dry_run=True)
        assert summary["cleared"] == 1  # would clear 1 existing
        assert summary["replayed"] == 2  # would replay 2 events
        assert summary["files_rebuilt"] == 2  # would rebuild 2 files

        # But archive is unchanged
        count = get_archive_count(conn)
        assert count == 1  # still has old.flac

    def test_dry_run_empty(self, conn):
        summary = rebuild_archive_from_events(conn, dry_run=True)
        assert summary["cleared"] == 0
        assert summary["replayed"] == 0
        assert summary["files_rebuilt"] == 0


# ── Edge cases ────────────────────────────────────────────────────────────────

class TestRebuildEdgeCases:
    def test_non_file_events_skipped(self, conn):
        """RUN_START, RUN_END events (no file_path) are skipped."""
        log_event(conn, "run_001", "RUN_START")
        log_event(conn, "run_001", "RUN_END")
        conn.commit()

        summary = rebuild_archive_from_events(conn)
        assert summary["files_rebuilt"] == 0
        assert summary["replayed"] == 2  # events were replayed, just no file effect

    def test_malformed_json_in_new_value(self, conn):
        """Non-JSON new_value for FILE_HASHED is handled gracefully."""
        log_event(conn, "run_001", "FILE_REGISTERED", file_path="/vault/x.flac")
        log_event(conn, "run_001", "FILE_HASHED", file_path="/vault/x.flac",
                  new_value="plain_hash_string")
        conn.commit()

        summary = rebuild_archive_from_events(conn)
        assert summary["files_rebuilt"] == 1
        row = conn.execute(
            "SELECT audio_hash FROM archive WHERE file_path=?",
            ("/vault/x.flac",),
        ).fetchone()
        # Falls back to storing the plain string as audio_hash
        assert row["audio_hash"] == "plain_hash_string"
