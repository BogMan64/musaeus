"""
Tests for TaggerStage — writes normalised metadata from DB back to file tags.

All mutagen interactions are mocked.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.tagger import TaggerStage


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
    defaults = {
        "file_path": file_path,
        "status": "CATALOGUED",
        "artist": "Canonical Artist",
        "album": "Canonical Album",
        "title": "Canonical Title",
        "genre": "Rock",
        "year": "2023",
        "track": 5,
    }
    defaults.update(kwargs)
    upsert_archive(ctx.conn, defaults)
    ctx.conn.commit()


# ── Validate ──────────────────────────────────────────────────────────────────


class TestTaggerValidate:
    def test_validate_with_mutagen(self, ctx):
        """Validate should pass if mutagen is importable."""
        with (
            patch("builtins.__import__", wraps=__import__),
            patch.dict("sys.modules", {"mutagen": MagicMock()}),
        ):
            TaggerStage().validate(ctx)

    def test_validate_without_mutagen(self, ctx):
        from musaeus.stages.base import StageError

        with (
            patch.dict("sys.modules", {"mutagen": None}),
            patch("builtins.__import__", side_effect=ImportError("no mutagen")),
            pytest.raises(StageError, match="mutagen"),
        ):
            TaggerStage().validate(ctx)


# ── _read_tags / _write_tags (mocked) ────────────────────────────────────────


class TestTagHelpers:
    @patch("musaeus.stages.tagger._read_tags")
    def test_compute_changes_detects_diff(self, mock_read, ctx, tmp_path):
        """If DB has different values than file tags, changes are detected."""
        track = tmp_path / "song.flac"
        track.write_bytes(b"AUDIO")

        stage = TaggerStage()
        db_row = {
            "file_path": str(track),
            "artist": "New Artist",
            "album": "New Album",
            "title": "New Title",
            "genre": "Jazz",
            "year": "2024",
            "track": "3",
        }
        file_tags = {
            "artist": "Old Artist",
            "album": "Old Album",
            "title": "Old Title",
            "genre": "Rock",
            "year": "2023",
            "track": "1",
        }
        changes = stage._compute_changes(db_row, file_tags)
        assert changes["artist"] == "New Artist"
        assert changes["album"] == "New Album"
        assert changes["title"] == "New Title"
        assert changes["genre"] == "Jazz"

    def test_compute_changes_no_diff(self, ctx):
        stage = TaggerStage()
        db_row = {
            "artist": "Same",
            "album": "Same",
            "title": "Same",
            "genre": "Rock",
            "year": "2023",
            "track": "5",
        }
        file_tags = {
            "artist": "Same",
            "album": "Same",
            "title": "Same",
            "genre": "Rock",
            "year": "2023",
            "track": "5",
        }
        changes = stage._compute_changes(db_row, file_tags)
        assert changes == {}

    def test_compute_changes_skips_empty_db_values(self, ctx):
        """If DB value is empty/None, don't write empty tags."""
        stage = TaggerStage()
        db_row = {
            "artist": None,
            "album": "",
            "title": "Title",
            "genre": "",
            "year": "",
            "track": "",
        }
        file_tags = {
            "artist": "Existing",
            "album": "Existing",
            "title": "Old",
            "genre": "R",
            "year": "2020",
            "track": "1",
        }
        changes = stage._compute_changes(db_row, file_tags)
        # Only title should change (it's non-empty and different)
        assert "title" in changes
        assert "artist" not in changes
        assert "album" not in changes


# ── TaggerStage dry_run ───────────────────────────────────────────────────────


class TestTaggerDryRun:
    @patch("musaeus.stages.tagger.TaggerStage.validate")
    @patch("musaeus.stages.tagger._read_tags")
    def test_dry_run_reports_changes(self, mock_read, mock_validate, ctx_dry, tmp_path):
        track = tmp_path / "song.flac"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx_dry, str(track))

        # File has different tags
        mock_read.return_value = {
            "artist": "Old Artist",
            "album": "Old Album",
            "title": "Old Title",
            "genre": "Pop",
            "year": "2020",
            "track": "1",
        }

        result = TaggerStage().dry_run(ctx_dry)
        assert result.dry_run is True
        assert result.files_changed == 1
        assert any("DRY RUN" in n for n in result.notes)

    @patch("musaeus.stages.tagger.TaggerStage.validate")
    @patch("musaeus.stages.tagger._read_tags")
    def test_dry_run_no_changes_needed(self, mock_read, mock_validate, ctx_dry, tmp_path):
        track = tmp_path / "clean.flac"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx_dry, str(track))

        mock_read.return_value = {
            "artist": "Canonical Artist",
            "album": "Canonical Album",
            "title": "Canonical Title",
            "genre": "Rock",
            "year": "2023",
            "track": "5",
        }

        result = TaggerStage().dry_run(ctx_dry)
        assert result.files_changed == 0
        assert result.files_skipped == 1


# ── TaggerStage run ───────────────────────────────────────────────────────────


class TestTaggerRun:
    @patch("musaeus.stages.tagger.TaggerStage.validate")
    @patch("musaeus.stages.tagger._write_tags")
    @patch("musaeus.stages.tagger._read_tags")
    def test_run_writes_changed_tags(self, mock_read, mock_write, mock_validate, ctx, tmp_path):
        track = tmp_path / "song.flac"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx, str(track))

        mock_read.return_value = {
            "artist": "Old",
            "album": "Old",
            "title": "Old",
            "genre": "Pop",
            "year": "2020",
            "track": "1",
        }
        mock_write.return_value = True

        result = TaggerStage().run(ctx)
        assert result.success is True
        assert result.files_changed == 1
        mock_write.assert_called_once()

    @patch("musaeus.stages.tagger.TaggerStage.validate")
    @patch("musaeus.stages.tagger._write_tags")
    @patch("musaeus.stages.tagger._read_tags")
    def test_run_skips_clean_files(self, mock_read, mock_write, mock_validate, ctx, tmp_path):
        track = tmp_path / "clean.flac"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx, str(track))

        mock_read.return_value = {
            "artist": "Canonical Artist",
            "album": "Canonical Album",
            "title": "Canonical Title",
            "genre": "Rock",
            "year": "2023",
            "track": "5",
        }

        result = TaggerStage().run(ctx)
        assert result.files_changed == 0
        assert result.files_skipped == 1
        mock_write.assert_not_called()

    @patch("musaeus.stages.tagger.TaggerStage.validate")
    @patch("musaeus.stages.tagger._write_tags")
    @patch("musaeus.stages.tagger._read_tags")
    def test_run_handles_write_failure(self, mock_read, mock_write, mock_validate, ctx, tmp_path):
        track = tmp_path / "fail.flac"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx, str(track))

        mock_read.return_value = {
            "artist": "Old",
            "album": "X",
            "title": "X",
            "genre": "",
            "year": "",
            "track": "",
        }
        mock_write.return_value = False

        result = TaggerStage().run(ctx)
        assert result.files_errored == 1
        assert result.success is False

    @patch("musaeus.stages.tagger.TaggerStage.validate")
    @patch("musaeus.stages.tagger._write_tags")
    @patch("musaeus.stages.tagger._read_tags")
    def test_run_missing_file_skipped(self, mock_read, mock_write, mock_validate, ctx, tmp_path):
        _insert_catalogued(ctx, str(tmp_path / "gone.flac"))

        result = TaggerStage().run(ctx)
        assert result.files_skipped == 1
        mock_read.assert_not_called()

    @patch("musaeus.stages.tagger.TaggerStage.validate")
    @patch("musaeus.stages.tagger._write_tags")
    @patch("musaeus.stages.tagger._read_tags")
    def test_run_logs_events(self, mock_read, mock_write, mock_validate, ctx, tmp_path):
        track = tmp_path / "song.mp3"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx, str(track))

        mock_read.return_value = {
            "artist": "Old",
            "album": "Old",
            "title": "Old",
            "genre": "",
            "year": "",
            "track": "",
        }
        mock_write.return_value = True

        TaggerStage().run(ctx)

        events = ctx.conn.execute("SELECT * FROM events WHERE event_type='TAGGER_WRITE'").fetchall()
        assert len(events) == 1


# ── albumartist repair ───────────────────────────────────────────────────────
#
# albumartist has no archive column and was never written by this stage, so it
# kept whatever spelling the source file arrived with. Confirmed live
# 2026-08-21: 2,035 of 5,894 article-artist files (34.5%) had artist and
# albumartist disagreeing. It is repaired, not mirrored -- a genuinely
# different albumartist (compilation / split credit) must survive untouched.


class TestAlbumArtistRepair:
    def _changes(self, db_artist, file_albumartist):
        return TaggerStage()._compute_changes(
            {"artist": db_artist},
            {"artist": db_artist, "albumartist": file_albumartist},
        )

    def test_leading_the_variant_is_corrected(self):
        assert self._changes("Cranberries, The", "The Cranberries") == {
            "albumartist": "Cranberries, The"
        }

    def test_parenthetical_variant_is_corrected(self):
        assert self._changes("Ronettes, The", "Ronettes (the)") == {"albumartist": "Ronettes, The"}

    def test_already_canonical_is_left_alone(self):
        assert self._changes("Beatles, The", "Beatles, The") == {}

    def test_various_artists_is_preserved(self):
        # A compilation's albumartist is genuinely not the track artist.
        assert self._changes("Beatles, The", "Various Artists") == {}

    def test_split_credit_is_preserved(self):
        assert self._changes("Johnny Cash", "Johnny Cash, The Tennessee Two") == {}

    def test_unrelated_albumartist_is_preserved(self):
        assert self._changes("Beatles, The", "Rolling Stones, The") == {}

    def test_empty_albumartist_is_not_invented(self):
        # Nothing to repair, and no DB column to source a value from.
        assert self._changes("Beatles, The", "") == {}

    def test_protected_stylized_name_not_touched(self):
        assert self._changes("De La Soul", "De La Soul") == {}
