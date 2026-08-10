"""
Tests for CanonicalizeStage — bring CATALOGUED files to ALAC-in-.m4a
(lossless sources) or 256k AAC-in-.m4a (sub-lossless sources), based on
real ffprobe codec rather than file extension.

Uses real ffmpeg-generated audio files (not mocked) because this stage's
entire purpose is real codec conversion -- a mock would hide exactly the
kind of bug these tests exist to catch (e.g. the "-f mp4" output-format
fix, discovered only by actually running ffmpeg against a real file).
Skips all tests if ffmpeg/ffprobe are not available.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.canonicalize import CanonicalizeStage

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not available",
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
    cfg.ensure_dirs()
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=False)


@pytest.fixture
def ctx_dry(cfg: MusicConfig) -> RunContext:
    cfg.ensure_dirs()
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=True)


def _gen_audio(path: Path, codec: str, duration: int = 2) -> None:
    """Generate a short synthetic audio file with a real codec via ffmpeg."""
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"sine=frequency=440:duration={duration}",
        "-c:a", codec, str(path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def _probe_codec(path: Path) -> str:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _register_catalogued(ctx: RunContext, path: Path, codec: str, ext: str) -> None:
    upsert_archive(ctx.conn, {
        "file_path": str(path),
        "filename": path.name,
        "ext": ext,
        "status": "CATALOGUED",
        "codec": codec,
        "artist": "Test Artist",
        "title": "Test Title",
        "bitrate": 128000,
        "sample_rate": 44100,
        "channels": 1,
        "duration": 2.0,
    })
    ctx.conn.commit()


# ── PASSTHROUGH ────────────────────────────────────────────────────────────────

class TestPassthrough:
    def test_alac_in_m4a_untouched(self, ctx):
        path = ctx.inbox / "already_alac.m4a"
        ctx.inbox.mkdir(parents=True, exist_ok=True)
        _gen_audio(path, "alac")
        _register_catalogued(ctx, path, "alac", ".m4a")

        result = CanonicalizeStage().execute(ctx)

        assert result.success is True
        assert result.files_changed == 1
        row = ctx.conn.execute("SELECT canon_action, file_path FROM archive").fetchone()
        assert row["canon_action"] == "PASSTHROUGH"
        assert row["file_path"] == str(path)
        assert path.exists()  # never rewritten

    def test_aac_in_m4a_untouched(self, ctx):
        path = ctx.inbox / "already_aac.m4a"
        ctx.inbox.mkdir(parents=True, exist_ok=True)
        _gen_audio(path, "aac")
        _register_catalogued(ctx, path, "aac", ".m4a")

        result = CanonicalizeStage().execute(ctx)

        row = ctx.conn.execute("SELECT canon_action FROM archive").fetchone()
        assert row["canon_action"] == "PASSTHROUGH"


# ── CONVERTED (lossless -> ALAC) ──────────────────────────────────────────────

class TestConvert:
    def test_flac_converted_to_alac(self, ctx):
        path = ctx.inbox / "source.flac"
        ctx.inbox.mkdir(parents=True, exist_ok=True)
        _gen_audio(path, "flac")
        _register_catalogued(ctx, path, "flac", ".flac")

        result = CanonicalizeStage().execute(ctx)

        assert result.success is True
        assert result.files_changed == 1

        row = ctx.conn.execute("SELECT canon_action, file_path, ext FROM archive").fetchone()
        assert row["canon_action"] == "CONVERTED"
        assert row["ext"] == ".m4a"
        new_path = Path(row["file_path"])
        assert new_path.suffix == ".m4a"
        assert new_path.exists()
        assert not path.exists()  # original .flac gone
        assert _probe_codec(new_path) == "alac"

    def test_duration_preserved(self, ctx):
        path = ctx.inbox / "source.flac"
        ctx.inbox.mkdir(parents=True, exist_ok=True)
        _gen_audio(path, "flac", duration=5)
        _register_catalogued(ctx, path, "flac", ".flac")

        CanonicalizeStage().execute(ctx)

        row = ctx.conn.execute("SELECT file_path FROM archive").fetchone()
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", row["file_path"]],
            capture_output=True, text=True, check=True,
        )
        assert abs(float(out.stdout.strip()) - 5.0) < 1.0


# ── TRANSCODED (sub-lossless -> AAC) ──────────────────────────────────────────

class TestTranscode:
    def test_mp3_transcoded_to_aac(self, ctx):
        path = ctx.inbox / "source.mp3"
        ctx.inbox.mkdir(parents=True, exist_ok=True)
        _gen_audio(path, "libmp3lame")
        _register_catalogued(ctx, path, "mp3", ".mp3")

        result = CanonicalizeStage().execute(ctx)

        assert result.success is True
        row = ctx.conn.execute("SELECT canon_action, file_path, ext FROM archive").fetchone()
        assert row["canon_action"] == "TRANSCODED"
        assert row["ext"] == ".m4a"
        new_path = Path(row["file_path"])
        assert new_path.exists()
        assert not path.exists()
        assert _probe_codec(new_path) == "aac"

    def test_transcode_logged_to_tunemymusic_csv(self, ctx):
        path = ctx.inbox / "source.mp3"
        ctx.inbox.mkdir(parents=True, exist_ok=True)
        _gen_audio(path, "libmp3lame")
        _register_catalogued(ctx, path, "mp3", ".mp3")

        CanonicalizeStage().execute(ctx)

        csv_path = ctx.config.tunemymusic_csv_path
        assert csv_path.exists()
        content = csv_path.read_text()
        assert "reason,codec,bitrate_kbps" in content  # header
        assert "source.mp3" in content
        assert "MP3" in content


# ── Idempotency / re-run behaviour ─────────────────────────────────────────────

class TestIdempotency:
    def test_already_canonicalized_file_skipped_on_rerun(self, ctx):
        path = ctx.inbox / "source.flac"
        ctx.inbox.mkdir(parents=True, exist_ok=True)
        _gen_audio(path, "flac")
        _register_catalogued(ctx, path, "flac", ".flac")

        first = CanonicalizeStage().execute(ctx)
        assert first.files_changed == 1

        second = CanonicalizeStage().execute(ctx)
        assert second.files_processed == 0
        assert second.files_changed == 0

    def test_force_reprocesses(self, ctx):
        path = ctx.inbox / "source.flac"
        ctx.inbox.mkdir(parents=True, exist_ok=True)
        _gen_audio(path, "flac")
        _register_catalogued(ctx, path, "flac", ".flac")

        CanonicalizeStage().execute(ctx)
        ctx.set("canonicalize_force", True)
        second = CanonicalizeStage().execute(ctx)
        # Already-ALAC-in-m4a after first run -> PASSTHROUGH on forced re-run
        assert second.files_processed == 1


# ── Error handling ────────────────────────────────────────────────────────────

class TestErrorHandling:
    def test_missing_file_reported_not_crash(self, ctx):
        missing = ctx.inbox / "gone.flac"
        _register_catalogued(ctx, missing, "flac", ".flac")

        result = CanonicalizeStage().execute(ctx)

        assert result.files_errored == 1
        assert any("missing" in e.lower() for e in result.errors)


# ── Dry run ───────────────────────────────────────────────────────────────────

class TestDryRun:
    def test_dry_run_makes_no_changes(self, ctx_dry):
        path = ctx_dry.inbox / "source.flac"
        ctx_dry.inbox.mkdir(parents=True, exist_ok=True)
        _gen_audio(path, "flac")
        _register_catalogued(ctx_dry, path, "flac", ".flac")

        result = CanonicalizeStage().execute(ctx_dry)

        assert result.dry_run is True
        assert any("CONVERTED" in n for n in result.notes)
        # Nothing should actually have been written.
        assert path.exists()
        assert not (ctx_dry.inbox / "source.m4a").exists()

        row = ctx_dry.conn.execute("SELECT canonicalized_at FROM archive").fetchone()
        assert row["canonicalized_at"] is None
