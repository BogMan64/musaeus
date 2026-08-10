"""
Tests for HealthStage — library-wide consistency and quality checks.

HealthStage is read-only against files; it only inspects DB rows and writes
issues to validation_issues table.
"""

from pathlib import Path

import pytest
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.health import HealthStage, _check_row


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
    return MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library",
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


def _insert_catalogued(ctx: RunContext, file_path: str, **kwargs) -> None:
    """Insert a CATALOGUED row with default good metadata."""
    defaults = {
        "file_path": file_path,
        "status": "CATALOGUED",
        "title": "Test Song",
        "artist": "Test Artist",
        "album": "Test Album",
        "genre": "Rock",
        "year": "2023",
        "track": 5,
        "duration": 240.0,
        "bitrate": 320000,
        "codec": "mp3",
        "ext": ".mp3",
        "audio_hash": "abc" * 20,
    }
    defaults.update(kwargs)
    upsert_archive(ctx.conn, defaults)
    ctx.conn.commit()


# ── _check_row unit tests ────────────────────────────────────────────────────

class TestCheckRow:
    def test_healthy_row_no_issues(self):
        row = {
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "genre": "Rock",
            "year": "2023",
            "track": 1,
            "duration": 200.0,
            "bitrate": 320000,
            "codec": "mp3",
            "ext": ".mp3",
            "audio_hash": "abc123",
        }
        issues = _check_row(row)
        assert issues == []

    def test_missing_title(self):
        row = {"title": None, "artist": "A", "album": "B", "genre": "R",
               "year": "2023", "track": 1, "duration": 100.0,
               "bitrate": 320000, "codec": "mp3", "ext": ".mp3", "audio_hash": "x"}
        issues = _check_row(row)
        codes = [i[0] for i in issues]
        assert "MISSING_TITLE" in codes

    def test_missing_artist(self):
        row = {"title": "S", "artist": "", "album": "B", "genre": "R",
               "year": "2023", "track": 1, "duration": 100.0,
               "bitrate": 320000, "codec": "mp3", "ext": ".mp3", "audio_hash": "x"}
        issues = _check_row(row)
        codes = [i[0] for i in issues]
        assert "MISSING_ARTIST" in codes

    def test_zero_duration(self):
        row = {"title": "S", "artist": "A", "album": "B", "genre": "R",
               "year": "2023", "track": 1, "duration": 0.0,
               "bitrate": 320000, "codec": "mp3", "ext": ".mp3", "audio_hash": "x"}
        issues = _check_row(row)
        codes = [i[0] for i in issues]
        assert "ZERO_DURATION" in codes

    def test_suspicious_bitrate_too_low(self):
        row = {"title": "S", "artist": "A", "album": "B", "genre": "R",
               "year": "2023", "track": 1, "duration": 100.0,
               "bitrate": 32000, "codec": "mp3", "ext": ".mp3", "audio_hash": "x"}
        issues = _check_row(row)
        codes = [i[0] for i in issues]
        assert "SUSPICIOUS_BITRATE" in codes

    def test_suspicious_bitrate_none(self):
        row = {"title": "S", "artist": "A", "album": "B", "genre": "R",
               "year": "2023", "track": 1, "duration": 100.0,
               "bitrate": None, "codec": "mp3", "ext": ".mp3", "audio_hash": "x"}
        issues = _check_row(row)
        codes = [i[0] for i in issues]
        assert "SUSPICIOUS_BITRATE" in codes

    def test_low_quality_warning(self):
        row = {"title": "S", "artist": "A", "album": "B", "genre": "R",
               "year": "2023", "track": 1, "duration": 100.0,
               "bitrate": 96000, "codec": "mp3", "ext": ".mp3", "audio_hash": "x"}
        issues = _check_row(row)
        codes = [i[0] for i in issues]
        assert "LOW_QUALITY" in codes

    def test_lossless_low_bitrate(self):
        """FLAC with bitrate < 300k → LOSSLESS_EXPECTED warning."""
        row = {"title": "S", "artist": "A", "album": "B", "genre": "R",
               "year": "2023", "track": 1, "duration": 100.0,
               "bitrate": 200000, "codec": "flac", "ext": ".flac", "audio_hash": "x"}
        issues = _check_row(row)
        codes = [i[0] for i in issues]
        assert "LOSSLESS_EXPECTED" in codes

    def test_no_audio_hash(self):
        row = {"title": "S", "artist": "A", "album": "B", "genre": "R",
               "year": "2023", "track": 1, "duration": 100.0,
               "bitrate": 320000, "codec": "mp3", "ext": ".mp3", "audio_hash": None}
        issues = _check_row(row)
        codes = [i[0] for i in issues]
        assert "NO_AUDIO_HASH" in codes

    def test_unknown_codec(self):
        row = {"title": "S", "artist": "A", "album": "B", "genre": "R",
               "year": "2023", "track": 1, "duration": 100.0,
               "bitrate": 320000, "codec": None, "ext": ".mp3", "audio_hash": "x"}
        issues = _check_row(row)
        codes = [i[0] for i in issues]
        assert "UNKNOWN_CODEC" in codes

    def test_missing_genre_is_warning(self):
        row = {"title": "S", "artist": "A", "album": "B", "genre": None,
               "year": "2023", "track": 1, "duration": 100.0,
               "bitrate": 320000, "codec": "mp3", "ext": ".mp3", "audio_hash": "x"}
        issues = _check_row(row)
        severities = {i[0]: i[1] for i in issues}
        assert severities.get("MISSING_GENRE") == "warning"


# ── HealthStage.validate() ────────────────────────────────────────────────────

class TestHealthValidate:
    def test_validate_empty(self, ctx):
        HealthStage().validate(ctx)  # no raise

    def test_validate_with_data(self, ctx, tmp_path):
        _insert_catalogued(ctx, str(tmp_path / "a.mp3"))
        HealthStage().validate(ctx)


# ── HealthStage dry_run ───────────────────────────────────────────────────────

class TestHealthDryRun:
    def test_dry_run_reports_issues(self, ctx_dry, tmp_path):
        _insert_catalogued(ctx_dry, str(tmp_path / "bad.mp3"), title=None, artist=None)
        result = HealthStage().execute(ctx_dry)

        assert result.dry_run is True
        assert result.files_changed >= 1
        assert any("Would log" in n for n in result.notes)

    def test_dry_run_no_db_writes(self, ctx_dry, tmp_path):
        _insert_catalogued(ctx_dry, str(tmp_path / "bad.mp3"), title=None)
        HealthStage().execute(ctx_dry)

        count = ctx_dry.conn.execute(
            "SELECT COUNT(*) FROM validation_issues"
        ).fetchone()[0]
        assert count == 0

    def test_dry_run_healthy_library(self, ctx_dry, tmp_path):
        _insert_catalogued(ctx_dry, str(tmp_path / "good.mp3"))
        result = HealthStage().execute(ctx_dry)
        assert any("healthy" in n.lower() or "No issues" in n for n in result.notes)


# ── HealthStage run ───────────────────────────────────────────────────────────

class TestHealthRun:
    def test_run_writes_issues(self, ctx, tmp_path):
        _insert_catalogued(ctx, str(tmp_path / "bad.mp3"), title=None, artist=None)
        result = HealthStage().execute(ctx)

        assert result.files_changed >= 1
        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM validation_issues"
        ).fetchone()[0]
        assert count >= 2  # MISSING_TITLE + MISSING_ARTIST

    def test_run_no_issues_on_good_data(self, ctx, tmp_path):
        _insert_catalogued(ctx, str(tmp_path / "good.mp3"))
        result = HealthStage().execute(ctx)
        assert result.files_changed == 0
        assert any("healthy" in n.lower() or "No issues" in n for n in result.notes)

    def test_run_multiple_files(self, ctx, tmp_path):
        _insert_catalogued(ctx, str(tmp_path / "a.mp3"))
        _insert_catalogued(ctx, str(tmp_path / "b.mp3"), genre=None)
        _insert_catalogued(ctx, str(tmp_path / "c.mp3"), title=None, duration=0.0)
        result = HealthStage().execute(ctx)

        # a.mp3 is clean, b has MISSING_GENRE, c has MISSING_TITLE + ZERO_DURATION
        assert result.files_processed == 3
        assert result.files_changed == 2  # b and c have issues
        assert result.files_skipped == 1  # a is clean

    def test_idempotent_rerun(self, ctx, tmp_path):
        """Re-running health check with same run_id doesn't create duplicates."""
        _insert_catalogued(ctx, str(tmp_path / "bad.mp3"), title=None)
        HealthStage().execute(ctx)
        # Run again with same run_id (same ctx)
        count_before = ctx.conn.execute("SELECT COUNT(*) FROM validation_issues").fetchone()[0]
        HealthStage().execute(ctx)
        count_after = ctx.conn.execute("SELECT COUNT(*) FROM validation_issues").fetchone()[0]
        assert count_after == count_before
