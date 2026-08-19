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
    _is_skip_error,
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
        import essentia.standard  # noqa: F401  -- confirm it's actually available here

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
