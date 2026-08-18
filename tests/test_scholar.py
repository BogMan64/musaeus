"""
Tests for ScholarStage — Stage 3: Extract metadata via ffprobe.

Mocks the _probe function so no actual ffprobe is needed.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.scholar import ScholarStage, _extract_meta, _get_hashed


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


def _insert_hashed(ctx: RunContext, file_path: str) -> None:
    """Insert a HASHED row into archive for testing."""
    upsert_archive(ctx.conn, {
        "file_path": file_path,
        "status": "HASHED",
        "audio_hash": "abc123",
    })
    ctx.conn.commit()


SAMPLE_PROBE_DATA = {
    "format": {
        "duration": "245.67",
        "bit_rate": "320000",
        "tags": {
            "title": "Test Song",
            "artist": "Test Artist",
            "album": "Test Album",
            "genre": "Rock",
            "date": "2023",
            "track": "5/12",
        },
    },
    "streams": [
        {
            "codec_type": "audio",
            "codec_name": "flac",
            "sample_rate": "44100",
            "channels": 2,
            "bit_rate": "900000",
            "tags": {},
        }
    ],
}


# ── _extract_meta ─────────────────────────────────────────────────────────────

class TestExtractMeta:
    def test_basic_extraction(self):
        meta = _extract_meta(SAMPLE_PROBE_DATA)
        assert meta["title"] == "Test Song"
        assert meta["artist"] == "Test Artist"
        assert meta["album"] == "Test Album"
        assert meta["genre"] == "Rock"
        assert meta["year"] == "2023"
        assert meta["track"] == 5
        assert meta["duration"] == pytest.approx(245.67)
        assert meta["bitrate"] == 900000  # stream-level takes priority
        assert meta["sample_rate"] == 44100
        assert meta["channels"] == 2
        assert meta["codec"] == "flac"

    def test_missing_tags_returns_none(self):
        minimal = {"format": {}, "streams": []}
        meta = _extract_meta(minimal)
        assert meta["title"] is None
        assert meta["artist"] is None
        assert meta["album"] is None
        assert meta["genre"] is None
        assert meta["year"] is None
        assert meta["track"] is None
        assert meta["duration"] is None

    def test_bitrate_is_integer(self):
        """Regression: bitrate must always be int or None."""
        meta = _extract_meta(SAMPLE_PROBE_DATA)
        assert isinstance(meta["bitrate"], int)

    def test_track_number_with_slash(self):
        """'5/12' → 5"""
        data = {
            "format": {"tags": {"track": "5/12"}},
            "streams": [{"codec_type": "audio", "tags": {}}],
        }
        meta = _extract_meta(data)
        assert meta["track"] == 5

    def test_year_truncated_to_4_chars(self):
        """'2023-05-15' → '2023'"""
        data = {
            "format": {"tags": {"date": "2023-05-15"}},
            "streams": [{"codec_type": "audio", "tags": {}}],
        }
        meta = _extract_meta(data)
        assert meta["year"] == "2023"

    def test_stream_tags_fallback(self):
        """Tags in stream are used if format tags are absent."""
        data = {
            "format": {},
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "mp3",
                    "tags": {"artist": "StreamArtist", "title": "StreamTitle"},
                }
            ],
        }
        meta = _extract_meta(data)
        assert meta["artist"] == "StreamArtist"
        assert meta["title"] == "StreamTitle"


# ── ScholarStage.validate() ───────────────────────────────────────────────────

class TestScholarValidate:
    def test_validate_no_hashed_is_fine(self, ctx):
        """No HASHED rows → validate passes (just a no-op warning)."""
        stage = ScholarStage()
        stage.validate(ctx)

    def test_validate_with_hashed(self, ctx, tmp_path):
        _insert_hashed(ctx, str(tmp_path / "track.flac"))
        ScholarStage().validate(ctx)


# ── ScholarStage.dry_run() ────────────────────────────────────────────────────

class TestScholarDryRun:
    def test_dry_run_reports_count(self, ctx_dry, tmp_path):
        _insert_hashed(ctx_dry, str(tmp_path / "a.flac"))
        _insert_hashed(ctx_dry, str(tmp_path / "b.mp3"))

        result = ScholarStage().execute(ctx_dry)
        assert result.dry_run is True
        assert result.files_processed == 2
        assert any("2" in n for n in result.notes)

    def test_dry_run_no_db_changes(self, ctx_dry, tmp_path):
        _insert_hashed(ctx_dry, str(tmp_path / "a.flac"))
        ScholarStage().execute(ctx_dry)

        row = ctx_dry.conn.execute(
            "SELECT status FROM archive WHERE file_path=?",
            (str(tmp_path / "a.flac"),),
        ).fetchone()
        assert row["status"] == "HASHED"  # unchanged


# ── ScholarStage.run() ────────────────────────────────────────────────────────

class TestScholarRun:
    @patch("musaeus.stages.scholar._probe")
    def test_catalogues_file(self, mock_probe, ctx, tmp_path):
        track = tmp_path / "song.flac"
        track.write_bytes(b"AUDIO")
        _insert_hashed(ctx, str(track))

        mock_probe.return_value = SAMPLE_PROBE_DATA
        result = ScholarStage().execute(ctx)

        assert result.success is True
        assert result.files_changed == 1
        row = ctx.conn.execute(
            "SELECT status, artist, title, bitrate FROM archive WHERE file_path=?",
            (str(track),),
        ).fetchone()
        assert row["status"] == "CATALOGUED"
        assert row["artist"] == "Test Artist"
        assert row["title"] == "Test Song"
        assert isinstance(row["bitrate"], int)

    @patch("musaeus.stages.scholar._probe")
    def test_stores_metadata_cache(self, mock_probe, ctx, tmp_path):
        track = tmp_path / "song.flac"
        track.write_bytes(b"AUDIO")
        _insert_hashed(ctx, str(track))

        mock_probe.return_value = SAMPLE_PROBE_DATA
        ScholarStage().execute(ctx)

        cache = ctx.conn.execute(
            "SELECT * FROM metadata_cache WHERE file_path=?",
            (str(track),),
        ).fetchone()
        assert cache is not None
        assert cache["artist"] == "Test Artist"
        assert cache["raw_json"] is not None

    @patch("musaeus.stages.scholar._probe")
    def test_handles_missing_file(self, mock_probe, ctx, tmp_path):
        _insert_hashed(ctx, str(tmp_path / "gone.flac"))

        result = ScholarStage().execute(ctx)
        assert result.files_errored == 1
        mock_probe.assert_not_called()

    @patch("musaeus.stages.scholar._probe")
    def test_probe_error_handled(self, mock_probe, ctx, tmp_path):
        from musaeus.stages.scholar import ProbeError

        track = tmp_path / "bad.flac"
        track.write_bytes(b"BAD")
        _insert_hashed(ctx, str(track))

        mock_probe.side_effect = ProbeError("ffprobe not found")
        result = ScholarStage().execute(ctx)

        assert result.files_errored == 1
        assert result.success is False

    @patch("musaeus.stages.scholar._probe")
    def test_events_logged(self, mock_probe, ctx, tmp_path):
        track = tmp_path / "song.mp3"
        track.write_bytes(b"AUDIO")
        _insert_hashed(ctx, str(track))

        mock_probe.return_value = SAMPLE_PROBE_DATA
        ScholarStage().execute(ctx)

        events = ctx.conn.execute(
            "SELECT * FROM events WHERE event_type='METADATA_EXTRACTED'"
        ).fetchall()
        assert len(events) == 1


# ── _get_hashed helper ────────────────────────────────────────────────────────

class TestGetHashed:
    def test_empty_db(self, ctx):
        assert _get_hashed(ctx.conn) == []

    def test_only_returns_hashed_status(self, ctx, tmp_path):
        _insert_hashed(ctx, str(tmp_path / "a.flac"))
        upsert_archive(ctx.conn, {"file_path": str(tmp_path / "b.flac"), "status": "PENDING"})
        ctx.conn.commit()

        results = _get_hashed(ctx.conn)
        paths = [r["file_path"] for r in results]
        assert str(tmp_path / "a.flac") in paths
        assert str(tmp_path / "b.flac") not in paths
