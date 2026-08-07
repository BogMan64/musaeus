"""
Tests for SentinelStage — Stage 2: Hash files and detect exact duplicates.

Mocks audio_hash_safe and file_hash to avoid needing ffmpeg installed.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import get_archive_count, open_db, upsert_archive
from musaeus.stages.sentinel import SentinelStage, _get_pending, _hash_group_for


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
    return MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        db_path=tmp_path / "musaeus.db",
    )


@pytest.fixture
def ctx(cfg: MusicConfig) -> RunContext:
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=False)


@pytest.fixture
def ctx_dry(cfg: MusicConfig) -> RunContext:
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=True)


def _insert_pending(ctx: RunContext, file_path: str) -> None:
    """Insert a PENDING row into archive for testing."""
    upsert_archive(ctx.conn, {"file_path": file_path, "status": "PENDING"})
    ctx.conn.commit()


# ── Validate ──────────────────────────────────────────────────────────────────

class TestSentinelValidate:
    def test_validate_no_pending_is_noop(self, ctx):
        """Validate passes even with no PENDING rows (just info log)."""
        stage = SentinelStage()
        stage.validate(ctx)  # should not raise

    def test_validate_with_pending(self, ctx, tmp_path):
        _insert_pending(ctx, str(tmp_path / "track.flac"))
        stage = SentinelStage()
        stage.validate(ctx)  # should not raise


# ── Dry run ───────────────────────────────────────────────────────────────────

class TestSentinelDryRun:
    def test_dry_run_reports_pending_count(self, ctx_dry, tmp_path):
        _insert_pending(ctx_dry, str(tmp_path / "a.flac"))
        _insert_pending(ctx_dry, str(tmp_path / "b.mp3"))
        stage = SentinelStage()
        result = stage.execute(ctx_dry)

        assert result.dry_run is True
        assert result.files_processed == 2
        assert result.files_changed == 2
        assert any("2 file(s)" in n for n in result.notes)

    def test_dry_run_no_db_changes(self, ctx_dry, tmp_path):
        _insert_pending(ctx_dry, str(tmp_path / "a.flac"))
        stage = SentinelStage()
        stage.execute(ctx_dry)

        # Status should still be PENDING (no hash written)
        row = ctx_dry.conn.execute(
            "SELECT status, audio_hash FROM archive WHERE file_path=?",
            (str(tmp_path / "a.flac"),),
        ).fetchone()
        assert row["status"] == "PENDING"
        assert row["audio_hash"] is None

    def test_dry_run_empty(self, ctx_dry):
        stage = SentinelStage()
        result = stage.execute(ctx_dry)
        assert result.files_processed == 0
        assert any("0 file(s)" in n for n in result.notes)

    def test_dry_run_truncates_long_list(self, ctx_dry, tmp_path):
        """If more than 10 pending files, notes mention 'more'."""
        for i in range(15):
            _insert_pending(ctx_dry, str(tmp_path / f"track{i:02d}.flac"))
        stage = SentinelStage()
        result = stage.execute(ctx_dry)
        assert any("more" in n for n in result.notes)


# ── Run (mocked hashing) ─────────────────────────────────────────────────────

class TestSentinelRun:
    @patch("musaeus.stages.sentinel.audio_hash_safe")
    @patch("musaeus.stages.sentinel.file_hash")
    def test_hashes_pending_file(self, mock_fh, mock_ah, ctx, tmp_path):
        # Create a real file so Path.exists() passes
        track = tmp_path / "track.flac"
        track.write_bytes(b"FAKE AUDIO")
        _insert_pending(ctx, str(track))

        mock_fh.return_value = "f" * 64
        mock_ah.return_value = ("a" * 64, None)

        stage = SentinelStage()
        result = stage.execute(ctx)

        assert result.success is True
        assert result.files_changed == 1
        row = ctx.conn.execute(
            "SELECT status, audio_hash, full_hash FROM archive WHERE file_path=?",
            (str(track),),
        ).fetchone()
        assert row["status"] == "HASHED"
        assert row["audio_hash"] == "a" * 64
        assert row["full_hash"] == "f" * 64

    @patch("musaeus.stages.sentinel.audio_hash_safe")
    @patch("musaeus.stages.sentinel.file_hash")
    def test_detects_exact_duplicate(self, mock_fh, mock_ah, ctx, tmp_path):
        """Two files with the same audio_hash → flagged as EXACT duplicate."""
        track_a = tmp_path / "a.flac"
        track_b = tmp_path / "b.flac"
        track_a.write_bytes(b"FAKE A")
        track_b.write_bytes(b"FAKE B")
        _insert_pending(ctx, str(track_a))
        _insert_pending(ctx, str(track_b))

        shared_hash = "deadbeef" * 8
        mock_fh.return_value = "f" * 64
        mock_ah.return_value = (shared_hash, None)

        stage = SentinelStage()
        result = stage.execute(ctx)

        assert result.files_changed == 2
        dupes = ctx.conn.execute("SELECT * FROM duplicates").fetchall()
        assert len(dupes) >= 2
        assert all(d["duplicate_type"] == "EXACT" for d in dupes)

    @patch("musaeus.stages.sentinel.audio_hash_safe")
    @patch("musaeus.stages.sentinel.file_hash")
    def test_handles_missing_file(self, mock_fh, mock_ah, ctx, tmp_path):
        """File removed between ingest and sentinel → errored."""
        _insert_pending(ctx, str(tmp_path / "gone.flac"))

        stage = SentinelStage()
        result = stage.execute(ctx)

        assert result.files_errored == 1
        assert any("Missing" in e for e in result.errors)

    @patch("musaeus.stages.sentinel.audio_hash_safe")
    @patch("musaeus.stages.sentinel.file_hash")
    def test_audio_hash_failure(self, mock_fh, mock_ah, ctx, tmp_path):
        """If audio_hash fails, full_hash is stored and file stays non-HASHED."""
        track = tmp_path / "bad.flac"
        track.write_bytes(b"BAD AUDIO")
        _insert_pending(ctx, str(track))

        mock_fh.return_value = "f" * 64
        mock_ah.return_value = (None, "ffmpeg not found")

        stage = SentinelStage()
        result = stage.execute(ctx)

        assert result.files_errored == 1
        row = ctx.conn.execute(
            "SELECT status, full_hash, audio_hash FROM archive WHERE file_path=?",
            (str(track),),
        ).fetchone()
        assert row["full_hash"] == "f" * 64
        assert row["audio_hash"] is None
        # Status should NOT have advanced to HASHED since audio hash failed
        assert row["status"] != "HASHED"

    @patch("musaeus.stages.sentinel.audio_hash_safe")
    @patch("musaeus.stages.sentinel.file_hash")
    def test_events_logged(self, mock_fh, mock_ah, ctx, tmp_path):
        track = tmp_path / "song.flac"
        track.write_bytes(b"DATA")
        _insert_pending(ctx, str(track))

        mock_fh.return_value = "f" * 64
        mock_ah.return_value = ("a" * 64, None)

        SentinelStage().execute(ctx)

        events = ctx.conn.execute(
            "SELECT event_type FROM events WHERE event_type='HASH_COMPUTED'"
        ).fetchall()
        assert len(events) == 1


# ── Helper functions ──────────────────────────────────────────────────────────

class TestSentinelHelpers:
    def test_get_pending_empty(self, ctx):
        assert _get_pending(ctx.conn) == []

    def test_get_pending_returns_pending_rows(self, ctx, tmp_path):
        _insert_pending(ctx, str(tmp_path / "x.flac"))
        upsert_archive(ctx.conn, {"file_path": str(tmp_path / "y.flac"), "status": "HASHED", "audio_hash": "abc"})
        ctx.conn.commit()
        pending = _get_pending(ctx.conn)
        paths = [r["file_path"] for r in pending]
        assert str(tmp_path / "x.flac") in paths
        # y.flac is HASHED but has audio_hash set, so should NOT appear
        # (it's not PENDING and audio_hash is not NULL)

    def test_hash_group_for(self, ctx, tmp_path):
        upsert_archive(ctx.conn, {"file_path": "/a.flac", "audio_hash": "abc123", "status": "HASHED"})
        upsert_archive(ctx.conn, {"file_path": "/b.flac", "audio_hash": "abc123", "status": "HASHED"})
        upsert_archive(ctx.conn, {"file_path": "/c.flac", "audio_hash": "other", "status": "HASHED"})
        ctx.conn.commit()
        group = _hash_group_for(ctx.conn, "abc123")
        assert len(group) == 2
        assert "/a.flac" in group
        assert "/b.flac" in group
