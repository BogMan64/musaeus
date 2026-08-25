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
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration}",
        "-c:a",
        codec,
        str(path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def _probe_codec(path: Path) -> str:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _register_catalogued(ctx: RunContext, path: Path, codec: str, ext: str) -> None:
    upsert_archive(
        ctx.conn,
        {
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
        },
    )
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

        CanonicalizeStage().execute(ctx)

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
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                row["file_path"],
            ],
            capture_output=True,
            text=True,
            check=True,
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
        assert "Title,Artist,Album" in content  # importable header, matches batch_*.csv
        assert "source" in content  # the track identity, not its filename
        # Codec is diagnostic and stays in the archive row; this file is
        # Title,Artist,Album so it can be imported.
        assert "MP3" not in content


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


# ── STAGING flow (Grey's 2026-08-11 design decision) ───────────────────────────


class TestStagingFlow:
    def test_converted_file_lands_in_staging_not_inbox(self, ctx):
        """CONVERT/TRANSCODE output must land in config.staging, never as
        a sibling of the source in INBOX. Canonicalize's job stops at a
        verified STAGING copy -- ALAC-Library placement is Finalize's."""
        path = ctx.inbox / "source.flac"
        ctx.inbox.mkdir(parents=True, exist_ok=True)
        _gen_audio(path, "flac")
        _register_catalogued(ctx, path, "flac", ".flac")

        CanonicalizeStage().execute(ctx)

        row = ctx.conn.execute("SELECT file_path FROM archive").fetchone()
        new_path = Path(row["file_path"])
        assert new_path.parent == ctx.staging
        assert not (ctx.inbox / "source.m4a").exists()

    def test_db_collision_leaves_source_and_staged_file_untouched(self, ctx):
        """A real UNIQUE collision on archive.file_path (another row
        already holding the exact STAGING path this row would take) must
        revert nothing on disk: the pre-conversion INBOX original is
        never deleted, and the verified STAGING copy is left in place
        for manual review rather than being wired into the DB."""
        path = ctx.inbox / "source.flac"
        ctx.inbox.mkdir(parents=True, exist_ok=True)
        _gen_audio(path, "flac")
        _register_catalogued(ctx, path, "flac", ".flac")

        row_id = ctx.conn.execute(
            "SELECT id FROM archive WHERE file_path = ?", (str(path),)
        ).fetchone()["id"]
        colliding_staging_path = ctx.staging / f"{row_id}_source.m4a"

        # A decoy row that already occupies the exact path this row's
        # verified STAGING copy would be recorded at. canonicalized_at is
        # set so _get_pending() doesn't also pick up this decoy itself
        # (it exists purely to hold the colliding file_path).
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(colliding_staging_path),
                "status": "CATALOGUED",
            },
        )
        ctx.conn.execute(
            "UPDATE archive SET canonicalized_at = datetime('now') WHERE file_path = ?",
            (str(colliding_staging_path),),
        )
        ctx.conn.commit()

        result = CanonicalizeStage().execute(ctx)

        assert result.files_errored == 1
        assert path.exists()  # original untouched
        assert colliding_staging_path.exists()  # staged copy left behind for review

        row = ctx.conn.execute(
            "SELECT canonicalized_at FROM archive WHERE id = ?", (row_id,)
        ).fetchone()
        assert row["canonicalized_at"] is None

        events = ctx.conn.execute(
            "SELECT event_type FROM events WHERE event_type='CANONICALIZE_DB_COLLISION'"
        ).fetchall()
        assert len(events) == 1

    def test_failed_verification_leaves_marked_file_in_staging(self, ctx, monkeypatch):
        """A verification failure must never silently delete the STAGING
        attempt or silently retry -- it's renamed to .FAILED_VERIFY and
        left there, with a CANONICALIZE_VERIFY_FAILED event logged, and
        the original INBOX source is never touched."""
        from musaeus.stages import canonicalize as canon_mod

        path = ctx.inbox / "source.flac"
        ctx.inbox.mkdir(parents=True, exist_ok=True)
        _gen_audio(path, "flac")
        _register_catalogued(ctx, path, "flac", ".flac")

        def _fail_verify(source, output):
            raise canon_mod.CanonicalizeError("simulated verification failure")

        monkeypatch.setattr(canon_mod, "_verify_conversion", _fail_verify)

        result = CanonicalizeStage().execute(ctx)

        assert result.files_errored == 1
        assert path.exists()  # original completely untouched

        failed = list(ctx.staging.glob("*.FAILED_VERIFY"))
        assert len(failed) == 1

        row = ctx.conn.execute("SELECT canonicalized_at FROM archive").fetchone()
        assert row["canonicalized_at"] is None

        events = ctx.conn.execute(
            "SELECT note FROM events WHERE event_type='CANONICALIZE_VERIFY_FAILED'"
        ).fetchall()
        assert len(events) == 1
        assert "simulated verification failure" in events[0]["note"]

    def test_full_pipeline_leaves_staging_empty(self, ctx):
        """End-to-end: real Canonicalize (flac -> STAGING) followed by
        real Finalize (STAGING -> ALAC-Library) must leave STAGING
        completely empty, matching Grey's design: STAGING should be
        empty at the end of any clean run, and anything left behind is
        itself a signal to check."""
        from musaeus.stages.finalize import FinalizeStage

        path = ctx.inbox / "source.flac"
        ctx.inbox.mkdir(parents=True, exist_ok=True)
        _gen_audio(path, "flac")
        _register_catalogued(ctx, path, "flac", ".flac")

        canon_result = CanonicalizeStage().execute(ctx)
        assert canon_result.success is True

        staged = list(ctx.staging.glob("*.m4a"))
        assert len(staged) == 1  # confirms the STAGING hop actually happened

        finalize_result = FinalizeStage().execute(ctx)
        assert finalize_result.success is True
        assert finalize_result.files_changed == 1

        assert list(ctx.staging.rglob("*")) == []  # nothing left behind

        row = ctx.conn.execute("SELECT file_path, finalized_at FROM archive").fetchone()
        assert row["finalized_at"] is not None
        final_path = Path(row["file_path"])
        assert final_path.exists()
        assert final_path.is_relative_to(ctx.alac_library)  # landed under ALAC-Library


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
