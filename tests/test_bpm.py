"""
Tests for BPMStage — BPM/key/energy/danceability extraction (Essentia) +
tag write. Standalone stage, not part of DEFAULT_PIPELINE.

Essentia/mutagen are mocked for the bulk of these tests (a real Essentia
analysis pass takes several seconds per file and this project's CI image
doesn't install the optional `bpm` extra) -- except TestRealEssentiaEndToEnd,
which exercises the real library against a real generated audio file and is
skipped automatically wherever essentia isn't installed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.base import StageError
from musaeus.stages.bpm import (
    BPMStage,
    _is_multichannel_error,
    _is_skip_error,
    _mark_multichannel_skipped,
    _tunemymusic_csv_has_path,
    read_existing_tags,
    write_bpm_tags,
)


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


def _insert_catalogued(ctx: RunContext, file_path: str, ext: str = ".m4a") -> None:
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


_FEATURES = {"bpm": 120.0, "musical_key": "C major", "energy": 0.5, "danceability": 0.7}


# ── Validate ──────────────────────────────────────────────────────────────────


class TestValidate:
    def test_raises_when_essentia_missing(self, ctx):
        with (
            patch.dict("sys.modules", {"essentia": None, "essentia.standard": None}),
            pytest.raises(StageError, match="essentia"),
        ):
            BPMStage().validate(ctx)

    def test_raises_when_mutagen_missing(self, ctx):
        # Needs essentia genuinely importable (this test is specifically
        # about mutagen being the missing one) -- skip where the optional
        # `bpm` extra isn't installed (e.g. CI), rather than failing.
        pytest.importorskip("essentia")

        with (
            patch.dict("sys.modules", {"mutagen": None}),
            pytest.raises(StageError, match="mutagen"),
        ):
            BPMStage().validate(ctx)


# ── read_existing_tags ───────────────────────────────────────────────────────


class TestReadExistingTags:
    def test_m4a_with_bpm_tag_returns_features(self, tmp_path: Path):
        path = tmp_path / "track.m4a"
        path.write_bytes(b"fake")
        fake_tags = {
            "tmpo": [128],
            "----:com.apple.iTunes:initialkey": [b"A minor"],
            "----:com.apple.iTunes:Energy": [b"0.80"],
            "----:com.apple.iTunes:Danceability": [b"0.60"],
        }
        mock_audio = MagicMock()
        mock_audio.tags = fake_tags
        with patch("mutagen.mp4.MP4", return_value=mock_audio):
            result = read_existing_tags(path)
        assert result == {
            "bpm": 128.0,
            "musical_key": "A minor",
            "energy": 0.80,
            "danceability": 0.60,
        }

    def test_m4a_without_bpm_tag_returns_none(self, tmp_path: Path):
        path = tmp_path / "track.m4a"
        path.write_bytes(b"fake")
        mock_audio = MagicMock()
        mock_audio.tags = {}
        with patch("mutagen.mp4.MP4", return_value=mock_audio):
            assert read_existing_tags(path) is None

    def test_m4a_no_tags_at_all_returns_none(self, tmp_path: Path):
        path = tmp_path / "track.m4a"
        path.write_bytes(b"fake")
        mock_audio = MagicMock()
        mock_audio.tags = None
        with patch("mutagen.mp4.MP4", return_value=mock_audio):
            assert read_existing_tags(path) is None

    def test_flac_with_bpm_tag_returns_features(self, tmp_path: Path):
        path = tmp_path / "track.flac"
        path.write_bytes(b"fake")
        mock_audio = MagicMock()
        mock_audio.tags = {
            "bpm": ["95"],
            "initialkey": ["D major"],
            "energy": ["0.40"],
            "danceability": ["0.30"],
        }
        with patch("mutagen.flac.FLAC", return_value=mock_audio):
            result = read_existing_tags(path)
        assert result == {
            "bpm": 95.0,
            "musical_key": "D major",
            "energy": 0.40,
            "danceability": 0.30,
        }

    def test_unsupported_extension_returns_none(self, tmp_path: Path):
        path = tmp_path / "track.wav"
        path.write_bytes(b"fake")
        assert read_existing_tags(path) is None

    def test_mutagen_exception_returns_none_not_raise(self, tmp_path: Path):
        path = tmp_path / "track.m4a"
        path.write_bytes(b"fake")
        with patch("mutagen.mp4.MP4", side_effect=RuntimeError("corrupt")):
            assert read_existing_tags(path) is None


# ── write_bpm_tags dispatch ───────────────────────────────────────────────────


class TestWriteBpmTagsDispatch:
    def test_m4a_calls_m4a_writer(self, tmp_path: Path):
        path = tmp_path / "track.m4a"
        with patch("musaeus.stages.bpm._write_tags_m4a", return_value=True) as m:
            assert write_bpm_tags(path, _FEATURES) is True
            m.assert_called_once_with(path, _FEATURES)

    def test_flac_calls_flac_writer(self, tmp_path: Path):
        path = tmp_path / "track.flac"
        with patch("musaeus.stages.bpm._write_tags_flac", return_value=True) as m:
            assert write_bpm_tags(path, _FEATURES) is True
            m.assert_called_once_with(path, _FEATURES)

    def test_unsupported_extension_is_db_only_not_a_failure(self, tmp_path: Path):
        path = tmp_path / "track.wav"
        assert write_bpm_tags(path, _FEATURES) is True


# ── _is_skip_error ────────────────────────────────────────────────────────────


class TestIsSkipError:
    @pytest.mark.parametrize(
        "msg",
        [
            "Track too short for analysis",
            "not enough audio frames",
            "OnsetDetectionGlobal output buffer is full",
        ],
    )
    def test_recognized_patterns(self, msg):
        assert _is_skip_error(Exception(msg)) is True

    def test_unrelated_error_not_a_skip(self):
        assert _is_skip_error(Exception("permission denied")) is False


# ── Stage run/dry_run ─────────────────────────────────────────────────────────


class TestBPMStageRun:
    def test_tag_shortcut_skips_essentia_and_does_not_rewrite_tags(self, ctx):
        path = ctx.inbox / "track.m4a"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake")
        _insert_catalogued(ctx, str(path))

        with (
            patch("musaeus.stages.bpm.read_existing_tags", return_value=_FEATURES),
            patch("musaeus.stages.bpm.analyze_file") as m_analyze,
            patch("musaeus.stages.bpm.write_bpm_tags") as m_write,
        ):
            result = BPMStage().run(ctx)

        assert result.success is True
        assert result.files_changed == 1
        m_analyze.assert_not_called()
        m_write.assert_not_called()  # already tagged -- don't rewrite

        row = ctx.conn.execute(
            "SELECT bpm, musical_key, bpm_analyzed_at FROM archive WHERE file_path = ?",
            (str(path),),
        ).fetchone()
        assert row["bpm"] == 120.0
        assert row["musical_key"] == "C major"
        assert row["bpm_analyzed_at"] is not None

    def test_essentia_path_writes_tags_and_saves(self, ctx):
        path = ctx.inbox / "track.m4a"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake")
        _insert_catalogued(ctx, str(path))

        with (
            patch("musaeus.stages.bpm.read_existing_tags", return_value=None),
            patch("musaeus.stages.bpm.analyze_file", return_value=_FEATURES) as m_analyze,
            patch("musaeus.stages.bpm.write_bpm_tags", return_value=True) as m_write,
        ):
            result = BPMStage().run(ctx)

        assert result.success is True
        assert result.files_changed == 1
        m_analyze.assert_called_once()
        m_write.assert_called_once()

        row = ctx.conn.execute(
            "SELECT bpm FROM archive WHERE file_path = ?", (str(path),)
        ).fetchone()
        assert row["bpm"] == 120.0

    def test_skip_error_counted_as_skipped_not_errored(self, ctx):
        path = ctx.inbox / "track.m4a"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake")
        _insert_catalogued(ctx, str(path))

        with (
            patch("musaeus.stages.bpm.read_existing_tags", return_value=None),
            patch(
                "musaeus.stages.bpm.analyze_file",
                side_effect=RuntimeError("track too short for analysis"),
            ),
        ):
            result = BPMStage().run(ctx)

        assert result.success is True
        assert result.files_skipped == 1
        assert result.files_errored == 0

    def test_genuine_analysis_error_counted_as_errored(self, ctx):
        path = ctx.inbox / "track.m4a"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake")
        _insert_catalogued(ctx, str(path))

        with (
            patch("musaeus.stages.bpm.read_existing_tags", return_value=None),
            patch("musaeus.stages.bpm.analyze_file", side_effect=RuntimeError("segfault-ish")),
        ):
            result = BPMStage().run(ctx)

        assert result.success is False
        assert result.files_errored == 1

    def test_missing_file_skipped(self, ctx):
        _insert_catalogued(ctx, str(ctx.inbox / "vanished.m4a"))
        result = BPMStage().run(ctx)
        assert result.files_skipped == 1
        assert result.files_errored == 0

    def test_already_analyzed_row_skipped_on_rerun_without_force(self, ctx):
        path = ctx.inbox / "track.m4a"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake")
        _insert_catalogued(ctx, str(path))

        with (
            patch("musaeus.stages.bpm.read_existing_tags", return_value=_FEATURES),
            patch("musaeus.stages.bpm.analyze_file"),
            patch("musaeus.stages.bpm.write_bpm_tags"),
        ):
            first = BPMStage().run(ctx)
            second = BPMStage().run(ctx)

        assert first.files_changed == 1
        assert second.files_processed == 0

    def test_force_reprocesses_already_analyzed_row(self, ctx):
        path = ctx.inbox / "track.m4a"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake")
        _insert_catalogued(ctx, str(path))
        ctx.set("bpm_force", True)

        with (
            patch("musaeus.stages.bpm.read_existing_tags", return_value=_FEATURES),
            patch("musaeus.stages.bpm.analyze_file"),
            patch("musaeus.stages.bpm.write_bpm_tags"),
        ):
            BPMStage().run(ctx)
            second = BPMStage().run(ctx)

        assert second.files_processed == 1

    def test_dry_run_makes_no_db_changes(self, ctx):
        path = ctx.inbox / "track.m4a"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake")
        _insert_catalogued(ctx, str(path))

        result = BPMStage().dry_run(ctx)

        assert result.files_processed == 1
        row = ctx.conn.execute(
            "SELECT bpm_analyzed_at FROM archive WHERE file_path = ?", (str(path),)
        ).fetchone()
        assert row["bpm_analyzed_at"] is None


# ── Real Essentia end-to-end (skipped if essentia isn't installed) ──────────


class TestRealEssentiaEndToEnd:
    def test_analyze_file_against_real_generated_audio(self, tmp_path: Path):
        pytest.importorskip("essentia")
        import shutil
        import subprocess

        if not shutil.which("ffmpeg"):
            pytest.skip("ffmpeg not available")

        path = tmp_path / "real.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=3",
                str(path),
            ],
            capture_output=True,
            check=True,
        )

        from musaeus.stages.bpm import analyze_file

        features = analyze_file(path)
        assert features["bpm"] > 0
        assert isinstance(features["musical_key"], str) and features["musical_key"]
        assert 0.0 <= features["energy"] <= 1.0
        assert features["danceability"] >= 0.0


# ── Multichannel skip (Grey, 2026-08-20: no interest in multichannel) ───────


def _insert_catalogued_with_channels(
    ctx: RunContext, file_path: str, channels: int | None, ext: str = ".m4a"
) -> None:
    _insert_catalogued(ctx, file_path, ext=ext)
    ctx.conn.execute(
        "UPDATE archive SET channels = ?, codec = 'aac', bitrate = 256000, "
        "sample_rate = 48000, duration = 200.0 WHERE file_path = ?",
        (channels, file_path),
    )
    ctx.conn.commit()


class TestMulitchannelDetection:
    def test_is_multichannel_error_matches(self):
        exc = RuntimeError(
            "Error while configuring MonoLoader: AudioLoader: could not load "
            "audio. Audio file has more than 2 channels."
        )
        assert _is_multichannel_error(exc) is True

    def test_is_multichannel_error_does_not_match_other_errors(self):
        assert _is_multichannel_error(RuntimeError("too short")) is False
        assert _is_multichannel_error(RuntimeError("empty signal")) is False


class TestTuneMyMusicDedup:
    def test_missing_csv_returns_false(self, tmp_path: Path):
        assert _tunemymusic_csv_has_path(tmp_path / "TuneMyMusic.csv", "/a.m4a") is False

    def test_finds_existing_path(self, tmp_path: Path):
        csv_path = tmp_path / "TuneMyMusic.csv"
        csv_path.write_text(
            "reason,codec,bitrate_kbps,sample_rate,channels,duration_sec,path\n"
            "multichannel,AAC,256,48000,6,200.0,/music/a.m4a\n"
        )
        assert _tunemymusic_csv_has_path(csv_path, "/music/a.m4a") is True
        assert _tunemymusic_csv_has_path(csv_path, "/music/b.m4a") is False


class TestMarkMultichannelSkipped:
    def test_sets_bpm_analyzed_at_with_null_features(self, ctx, tmp_path):
        track = str(tmp_path / "surround.m4a")
        _insert_catalogued_with_channels(ctx, track, channels=6)

        _mark_multichannel_skipped(ctx, track)

        row = ctx.conn.execute(
            "SELECT bpm, musical_key, energy, danceability, bpm_analyzed_at "
            "FROM archive WHERE file_path=?",
            (track,),
        ).fetchone()
        assert row["bpm_analyzed_at"] is not None
        assert row["bpm"] is None
        assert row["musical_key"] is None

    def test_logs_bpm_skipped_multichannel_event(self, ctx, tmp_path):
        track = str(tmp_path / "surround.m4a")
        _insert_catalogued_with_channels(ctx, track, channels=6)

        _mark_multichannel_skipped(ctx, track)

        events = ctx.conn.execute(
            "SELECT * FROM events WHERE event_type='BPM_SKIPPED_MULTICHANNEL'"
        ).fetchall()
        assert len(events) == 1
        assert events[0]["file_path"] == track

    def test_appends_tunemymusic_row(self, ctx, tmp_path):
        track = str(tmp_path / "surround.m4a")
        _insert_catalogued_with_channels(ctx, track, channels=6)

        _mark_multichannel_skipped(ctx, track)

        csv_path = ctx.config.tunemymusic_csv_path
        content = csv_path.read_text()
        assert track in content
        assert "multichannel" in content.lower()
        assert "6" in content  # channels column

    def test_does_not_duplicate_row_on_second_call(self, ctx, tmp_path):
        track = str(tmp_path / "surround.m4a")
        _insert_catalogued_with_channels(ctx, track, channels=6)

        _mark_multichannel_skipped(ctx, track)
        _mark_multichannel_skipped(ctx, track)

        csv_path = ctx.config.tunemymusic_csv_path
        assert csv_path.read_text().count(track) == 1


class TestProcessOneMultichannel:
    @patch("musaeus.stages.bpm.BPMStage.validate")
    def test_proactive_db_channels_skip_never_calls_essentia(self, mock_validate, ctx, tmp_path):
        track = str(tmp_path / "surround.m4a")
        Path(track).write_bytes(b"fake")
        _insert_catalogued_with_channels(ctx, track, channels=6)

        with patch("musaeus.stages.bpm.analyze_file") as mock_analyze:
            status = BPMStage()._process_one(ctx, track, retag=False)

        assert status == "skip_multichannel"
        mock_analyze.assert_not_called()

    @patch("musaeus.stages.bpm.BPMStage.validate")
    def test_essentia_exception_fallback_also_marks_multichannel(
        self, mock_validate, ctx, tmp_path
    ):
        """channels is NULL/unset in the DB but Essentia itself reports
        the file has more than 2 channels -- the exception-based fallback
        must catch this too, not just the proactive DB check."""
        track = str(tmp_path / "surround.m4a")
        Path(track).write_bytes(b"fake")
        _insert_catalogued_with_channels(ctx, track, channels=None)

        with patch(
            "musaeus.stages.bpm.analyze_file",
            side_effect=RuntimeError(
                "AudioLoader: could not load audio. Audio file has more than 2 channels."
            ),
        ):
            status = BPMStage()._process_one(ctx, track, retag=False)

        assert status == "skip_multichannel"
        row = ctx.conn.execute(
            "SELECT bpm_analyzed_at FROM archive WHERE file_path=?", (track,)
        ).fetchone()
        assert row["bpm_analyzed_at"] is not None


class TestRunWithMultichannel:
    @patch("musaeus.stages.bpm.BPMStage.validate")
    def test_run_does_not_fail_stage_on_multichannel_skip(self, mock_validate, ctx, tmp_path):
        track = str(tmp_path / "surround.m4a")
        Path(track).write_bytes(b"fake")
        _insert_catalogued_with_channels(ctx, track, channels=6)

        result = BPMStage().run(ctx)

        assert result.success is True
        assert result.files_skipped == 1
        assert any("skip_multichannel: 1" in n for n in result.notes)

    @patch("musaeus.stages.bpm.BPMStage.validate")
    def test_second_run_excludes_already_skipped_file(self, mock_validate, ctx, tmp_path):
        """bpm_analyzed_at being set means _get_pending naturally excludes
        it from every future run -- a true permanent bypass, not just a
        per-run reclassification."""
        track = str(tmp_path / "surround.m4a")
        Path(track).write_bytes(b"fake")
        _insert_catalogued_with_channels(ctx, track, channels=6)

        BPMStage().run(ctx)
        result = BPMStage().run(ctx)

        assert result.files_processed == 0
        assert any("nothing to do" in n for n in result.notes)
