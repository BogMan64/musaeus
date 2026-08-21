"""
Tests for ForgeStage — EBU R128 loudness measurement + ReplayGain tag embedding.

All external dependencies (ffmpeg, mutagen) are mocked.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.forge import ForgeStage, read_existing_rg_tags, write_rg_tags


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


def _insert_catalogued(ctx: RunContext, file_path: str, ext: str = ".flac") -> None:
    upsert_archive(
        ctx.conn,
        {
            "file_path": file_path,
            "status": "CATALOGUED",
            "ext": ext,
            "artist": "Artist",
            "album": "Album",
            "title": "Song",
        },
    )
    ctx.conn.commit()


# ── Validate ──────────────────────────────────────────────────────────────────


class TestForgeValidate:
    @patch("shutil.which", return_value=None)
    def test_validate_no_ffmpeg(self, mock_which, ctx):
        from musaeus.stages.base import StageError

        stage = ForgeStage()
        with pytest.raises(StageError, match="ffmpeg"):
            stage.validate(ctx)

    @patch("shutil.which", side_effect=lambda cmd: "/usr/bin/" + cmd)
    def test_validate_no_mutagen(self, mock_which, ctx):
        from musaeus.stages.base import StageError

        with patch.dict("sys.modules", {"mutagen": None}):
            stage = ForgeStage()
            with pytest.raises(StageError, match="mutagen"):
                stage.validate(ctx)

    @patch("shutil.which", side_effect=lambda cmd: "/usr/bin/" + cmd)
    def test_validate_passes_with_all_deps(self, mock_which, ctx):
        """If ffmpeg, ffprobe and mutagen are available, validate passes."""
        import sys

        # Ensure mutagen is importable (mock it)
        mock_mutagen = MagicMock()
        with patch.dict(sys.modules, {"mutagen": mock_mutagen}):
            stage = ForgeStage()
            stage.validate(ctx)  # should not raise


# ── Dry run ───────────────────────────────────────────────────────────────────


class TestForgeDryRun:
    @patch("musaeus.stages.forge.ForgeStage.validate")
    def test_dry_run_reports_pending(self, mock_validate, ctx_dry, tmp_path):
        track = tmp_path / "song.flac"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx_dry, str(track))

        stage = ForgeStage()
        result = stage.dry_run(ctx_dry)

        assert result.dry_run is True
        assert result.files_processed == 1
        assert any("DRY RUN" in n for n in result.notes)
        assert any("1 file(s)" in n for n in result.notes)

    @patch("musaeus.stages.forge.ForgeStage.validate")
    def test_dry_run_no_db_changes(self, mock_validate, ctx_dry, tmp_path):
        track = tmp_path / "song.flac"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx_dry, str(track))

        ForgeStage().dry_run(ctx_dry)

        row = ctx_dry.conn.execute(
            "SELECT rg_tagged_at FROM archive WHERE file_path=?",
            (str(track),),
        ).fetchone()
        assert row["rg_tagged_at"] is None

    @patch("musaeus.stages.forge.ForgeStage.validate")
    def test_dry_run_empty(self, mock_validate, ctx_dry):
        result = ForgeStage().dry_run(ctx_dry)
        assert result.files_processed == 0


# ── Run (mocked loudness + tags) ──────────────────────────────────────────────


class TestForgeRun:
    @patch("musaeus.stages.forge.ForgeStage.validate")
    @patch("musaeus.stages.forge.write_rg_tags")
    @patch("musaeus.stages.forge.measure_loudness")
    def test_run_measures_and_tags(self, mock_measure, mock_write, mock_validate, ctx, tmp_path):
        track = tmp_path / "song.flac"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx, str(track))

        mock_measure.return_value = (-14.0, -1.5, "ok")
        mock_write.return_value = True

        stage = ForgeStage()
        result = stage.run(ctx)

        assert result.success is True
        assert result.files_changed == 1
        mock_measure.assert_called_once()
        mock_write.assert_called_once()

        # Check DB was updated
        row = ctx.conn.execute(
            "SELECT lufs, rg_tagged_at FROM archive WHERE file_path=?",
            (str(track),),
        ).fetchone()
        assert row["lufs"] == pytest.approx(-14.0)
        assert row["rg_tagged_at"] is not None

    @patch("musaeus.stages.forge.ForgeStage.validate")
    @patch("musaeus.stages.forge.write_rg_tags")
    @patch("musaeus.stages.forge.measure_loudness")
    def test_run_silence_skipped(self, mock_measure, mock_write, mock_validate, ctx, tmp_path):
        track = tmp_path / "silence.flac"
        track.write_bytes(b"SILENCE")
        _insert_catalogued(ctx, str(track))

        mock_measure.return_value = (None, None, "silence")

        result = ForgeStage().run(ctx)
        assert result.files_skipped == 1
        mock_write.assert_not_called()

    @patch("musaeus.stages.forge.ForgeStage.validate")
    @patch("musaeus.stages.forge.write_rg_tags")
    @patch("musaeus.stages.forge.measure_loudness")
    def test_run_ffmpeg_fail(self, mock_measure, mock_write, mock_validate, ctx, tmp_path):
        track = tmp_path / "bad.flac"
        track.write_bytes(b"BAD")
        _insert_catalogued(ctx, str(track))

        mock_measure.return_value = (None, None, "ffmpeg_fail")

        result = ForgeStage().run(ctx)
        assert result.files_errored == 1
        assert result.success is False

    @patch("musaeus.stages.forge.ForgeStage.validate")
    @patch("musaeus.stages.forge.write_rg_tags")
    @patch("musaeus.stages.forge.measure_loudness")
    def test_run_skips_already_forged(self, mock_measure, mock_write, mock_validate, ctx, tmp_path):
        """Files with rg_tagged_at set are skipped unless force=True."""
        track = tmp_path / "done.flac"
        track.write_bytes(b"DONE")
        _insert_catalogued(ctx, str(track))
        # Mark as already forged
        ctx.conn.execute(
            "UPDATE archive SET rg_tagged_at='2024-01-01T00:00:00Z' WHERE file_path=?",
            (str(track),),
        )
        ctx.conn.commit()

        result = ForgeStage().run(ctx)
        # Nothing to do — already forged
        assert result.files_processed == 0
        mock_measure.assert_not_called()

    @patch("musaeus.stages.forge.ForgeStage.validate")
    @patch("musaeus.stages.forge.write_rg_tags")
    @patch("musaeus.stages.forge.measure_loudness")
    def test_run_force_retags(self, mock_measure, mock_write, mock_validate, ctx, tmp_path):
        """With forge_force=True, already-forged files are re-processed."""
        track = tmp_path / "done.flac"
        track.write_bytes(b"DONE")
        _insert_catalogued(ctx, str(track))
        ctx.conn.execute(
            "UPDATE archive SET rg_tagged_at='2024-01-01T00:00:00Z' WHERE file_path=?",
            (str(track),),
        )
        ctx.conn.commit()
        ctx.set("forge_force", True)

        mock_measure.return_value = (-16.0, -2.0, "ok")
        mock_write.return_value = True

        result = ForgeStage().run(ctx)
        assert result.files_changed == 1


# ── read_existing_rg_tags ────────────────────────────────────────────────────


class TestReadExistingRgTags:
    def test_m4a_with_r128_gain_returns_recovered_lufs(self, tmp_path: Path):
        path = tmp_path / "track.m4a"
        path.write_bytes(b"fake")
        mock_audio = MagicMock()
        # -3.5 dB gain @ -23 LUFS reference, Q7.8 fixed-point encoding
        mock_audio.get = {"com.apple.iTunes.R128_TRACK_GAIN": [str(int(round(-3.5 * 256)))]}.get
        with patch("mutagen.mp4.MP4", return_value=mock_audio):
            result = read_existing_rg_tags(path)
        assert result is not None
        assert result["lufs"] == pytest.approx(-19.5, abs=0.01)
        assert result["lufs_tp"] is None
        assert result["rg_peak"] is None
        assert result["rg_gain"] == pytest.approx(-18.0 - (-19.5), abs=0.01)

    def test_m4a_without_tag_returns_none(self, tmp_path: Path):
        path = tmp_path / "track.m4a"
        path.write_bytes(b"fake")
        mock_audio = MagicMock()
        mock_audio.get = {}.get
        with patch("mutagen.mp4.MP4", return_value=mock_audio):
            assert read_existing_rg_tags(path) is None

    def test_flac_with_full_rg_tags_returns_all_fields(self, tmp_path: Path):
        path = tmp_path / "track.flac"
        path.write_bytes(b"fake")
        mock_audio = MagicMock()
        mock_audio.get = {
            "replaygain_track_gain": ["+2.50 dB"],
            "replaygain_track_peak": ["0.98"],
            "replaygain_reference_loudness": ["18.00 LUFS"],
        }.get
        with patch("mutagen.flac.FLAC", return_value=mock_audio):
            result = read_existing_rg_tags(path)
        assert result == {
            "lufs": pytest.approx(-20.5),
            "lufs_tp": None,
            "rg_gain": pytest.approx(2.5),
            "rg_peak": pytest.approx(0.98),
        }

    def test_flac_without_reference_tag_defaults_to_r128_reference(self, tmp_path: Path):
        path = tmp_path / "track.flac"
        path.write_bytes(b"fake")
        mock_audio = MagicMock()
        mock_audio.get = {"replaygain_track_gain": ["-1.00 dB"]}.get
        with patch("mutagen.flac.FLAC", return_value=mock_audio):
            result = read_existing_rg_tags(path)
        assert result is not None
        assert result["lufs"] == pytest.approx(-17.0)
        assert result["rg_peak"] is None

    def test_flac_without_gain_tag_returns_none(self, tmp_path: Path):
        path = tmp_path / "track.flac"
        path.write_bytes(b"fake")
        mock_audio = MagicMock()
        mock_audio.get = {}.get
        with patch("mutagen.flac.FLAC", return_value=mock_audio):
            assert read_existing_rg_tags(path) is None

    def test_mp3_with_rg_tags_returns_fields(self, tmp_path: Path):
        path = tmp_path / "track.mp3"
        path.write_bytes(b"fake")
        mock_audio = MagicMock()
        mock_audio.get = {
            "replaygain_track_gain": ["+4.00 dB"],
            "replaygain_track_peak": ["0.5"],
        }.get
        with patch("mutagen.easyid3.EasyID3", return_value=mock_audio):
            result = read_existing_rg_tags(path)
        assert result is not None
        assert result["rg_gain"] == pytest.approx(4.0)
        assert result["rg_peak"] == pytest.approx(0.5)

    def test_wav_returns_none(self, tmp_path: Path):
        """WAV has no standard RG tag container -- always re-measure."""
        path = tmp_path / "track.wav"
        path.write_bytes(b"fake")
        assert read_existing_rg_tags(path) is None

    def test_aiff_returns_none(self, tmp_path: Path):
        """AIFF's gain-only TXXX tag isn't parsed as a shortcut source."""
        path = tmp_path / "track.aiff"
        path.write_bytes(b"fake")
        assert read_existing_rg_tags(path) is None

    def test_mutagen_exception_returns_none_not_raise(self, tmp_path: Path):
        path = tmp_path / "track.m4a"
        path.write_bytes(b"fake")
        with patch("mutagen.mp4.MP4", side_effect=RuntimeError("corrupt")):
            assert read_existing_rg_tags(path) is None


# ── Tag-shortcut integration (ForgeStage.run) ────────────────────────────────


class TestForgeTagShortcut:
    @patch("musaeus.stages.forge.ForgeStage.validate")
    @patch("musaeus.stages.forge.measure_loudness")
    @patch("musaeus.stages.forge.read_existing_rg_tags")
    def test_run_uses_tag_shortcut_skips_ffmpeg(
        self, mock_read, mock_measure, mock_validate, ctx, tmp_path
    ):
        track = tmp_path / "song.m4a"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx, str(track), ext=".m4a")
        mock_read.return_value = {"lufs": -19.5, "lufs_tp": None, "rg_gain": 1.5, "rg_peak": None}

        result = ForgeStage().run(ctx)

        assert result.success is True
        assert result.files_changed == 1
        mock_measure.assert_not_called()

        row = ctx.conn.execute(
            "SELECT lufs, rg_gain, rg_peak, rg_tagged_at FROM archive WHERE file_path=?",
            (str(track),),
        ).fetchone()
        assert row["lufs"] == pytest.approx(-19.5)
        assert row["rg_peak"] is None
        assert row["rg_tagged_at"] is not None

    @patch("musaeus.stages.forge.ForgeStage.validate")
    @patch("musaeus.stages.forge.write_rg_tags")
    @patch("musaeus.stages.forge.measure_loudness")
    @patch("musaeus.stages.forge.read_existing_rg_tags")
    def test_run_no_tag_falls_through_to_ffmpeg(
        self, mock_read, mock_measure, mock_write, mock_validate, ctx, tmp_path
    ):
        track = tmp_path / "song.flac"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx, str(track))
        mock_read.return_value = None
        mock_measure.return_value = (-14.0, -1.5, "ok")
        mock_write.return_value = True

        result = ForgeStage().run(ctx)

        assert result.files_changed == 1
        mock_measure.assert_called_once()

    @patch("musaeus.stages.forge.ForgeStage.validate")
    @patch("musaeus.stages.forge.write_rg_tags")
    @patch("musaeus.stages.forge.measure_loudness")
    @patch("musaeus.stages.forge.read_existing_rg_tags")
    def test_retag_bypasses_tag_shortcut(
        self, mock_read, mock_measure, mock_write, mock_validate, ctx, tmp_path
    ):
        track = tmp_path / "song.m4a"
        track.write_bytes(b"AUDIO")
        _insert_catalogued(ctx, str(track), ext=".m4a")
        ctx.set("forge_retag", True)
        mock_measure.return_value = (-16.0, -2.0, "ok")
        mock_write.return_value = True

        result = ForgeStage().run(ctx)

        mock_read.assert_not_called()
        mock_measure.assert_called_once()
        assert result.files_changed == 1


# ── write_rg_tags dispatch ────────────────────────────────────────────────────


class TestWriteRgTags:
    @patch("musaeus.stages.forge._write_tags_flac")
    def test_flac_dispatch(self, mock_flac, tmp_path):
        path = tmp_path / "song.flac"
        path.write_bytes(b"")
        mock_flac.return_value = True
        assert write_rg_tags(path, 4.5, 0.95) is True
        mock_flac.assert_called_once_with(path, 4.5, 0.95)

    @patch("musaeus.stages.forge._write_tags_mp3")
    def test_mp3_dispatch(self, mock_mp3, tmp_path):
        path = tmp_path / "song.mp3"
        path.write_bytes(b"")
        mock_mp3.return_value = True
        assert write_rg_tags(path, 3.0, 0.9) is True
        mock_mp3.assert_called_once_with(path, 3.0, 0.9)

    @patch("musaeus.stages.forge._write_tags_m4a")
    def test_m4a_dispatch(self, mock_m4a, tmp_path):
        path = tmp_path / "song.m4a"
        path.write_bytes(b"")
        mock_m4a.return_value = True
        # With r128_gain provided
        assert write_rg_tags(path, 4.0, 0.9, r128_gain=5.0) is True
        mock_m4a.assert_called_once_with(path, 5.0, 0.9)

    def test_wav_returns_true(self, tmp_path):
        """WAV has no tag writer — returns True (DB-only)."""
        path = tmp_path / "song.wav"
        path.write_bytes(b"")
        assert write_rg_tags(path, 4.0, 0.9) is True
