"""
Tests for IngestStage — Stage 1 of the Musaeus pipeline.

Creates a temporary vault with fake audio files, runs IngestStage,
and verifies DB state. No actual audio content needed for ingest.
"""

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import get_archive_count, open_db
from musaeus.stages.base import StageError
from musaeus.stages.ingest import IngestStage, _scan_inbox


@pytest.fixture
def vault(tmp_path: Path):
    """Set up a minimal vault structure with a populated inbox."""
    inbox = tmp_path / "INBOX"
    inbox.mkdir()

    # Create fake audio files (content doesn't matter for ingest)
    (inbox / "track01.flac").write_bytes(b"FAKE FLAC DATA")
    (inbox / "track02.mp3").write_bytes(b"FAKE MP3 DATA")
    (inbox / "notes.txt").write_bytes(b"not audio")  # should be ignored

    artist_dir = inbox / "Artist A" / "Album 1"
    artist_dir.mkdir(parents=True)
    (artist_dir / "01 - Song.flac").write_bytes(b"FAKE FLAC")

    return tmp_path


@pytest.fixture
def cfg(vault: Path) -> MusicConfig:
    return MusicConfig(
        vault_root=vault,
        inbox=vault / "INBOX",
        staging=vault / "STAGING",
        quarantine=vault / "QUARANTINE",
        runs_root=vault / "RUNS",
        meta_dir=vault / "MetaData",
        alac_library=vault / "ALAC-Library",
        db_path=vault / "musaeus.db",
    )


@pytest.fixture
def ctx(cfg: MusicConfig) -> RunContext:
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=False)


@pytest.fixture
def ctx_dry(cfg: MusicConfig) -> RunContext:
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=True)


# ── _scan_inbox ───────────────────────────────────────────────────────────────

class TestScanInbox:
    def test_finds_audio_files(self, vault):
        inbox = vault / "INBOX"
        found = _scan_inbox(inbox)
        names = {p.name for p in found}
        assert "track01.flac" in names
        assert "track02.mp3" in names
        assert "01 - Song.flac" in names

    def test_ignores_non_audio(self, vault):
        inbox = vault / "INBOX"
        found = _scan_inbox(inbox)
        names = {p.name for p in found}
        assert "notes.txt" not in names

    def test_returns_sorted(self, vault):
        inbox = vault / "INBOX"
        found = _scan_inbox(inbox)
        assert found == sorted(found)

    def test_missing_inbox_returns_empty(self, tmp_path):
        found = _scan_inbox(tmp_path / "NONEXISTENT")
        assert found == []


# ── IngestStage.validate() ────────────────────────────────────────────────────

class TestIngestValidate:
    def test_passes_with_existing_inbox(self, ctx):
        stage = IngestStage()
        stage.validate(ctx)  # should not raise

    def test_raises_if_no_inbox(self, cfg, tmp_path):
        cfg2 = MusicConfig(
            vault_root=tmp_path,
            inbox=tmp_path / "MISSING_INBOX",
            staging=tmp_path / "STAGING",
            quarantine=tmp_path / "QUARANTINE",
            runs_root=tmp_path / "RUNS",
            meta_dir=tmp_path / "MetaData",
            alac_library=tmp_path / "ALAC-Library",
            db_path=tmp_path / "musaeus.db",
        )
        conn = open_db(cfg2.db_path)
        bad_ctx = RunContext.new(cfg2, conn, dry_run=False)
        stage = IngestStage()
        with pytest.raises(StageError, match="Inbox"):
            stage.validate(bad_ctx)
        conn.close()


# ── IngestStage.run() ─────────────────────────────────────────────────────────

class TestIngestRun:
    def test_registers_audio_files(self, ctx):
        stage = IngestStage()
        result = stage.execute(ctx)

        assert result.success
        assert result.files_changed == 3  # track01.flac, track02.mp3, 01 - Song.flac
        assert get_archive_count(ctx.conn) == 3

    def test_skips_txt_files(self, ctx):
        stage = IngestStage()
        stage.execute(ctx)
        # notes.txt not in DB
        rows = ctx.conn.execute("SELECT file_path FROM archive").fetchall()
        paths = [r["file_path"] for r in rows]
        assert not any("notes.txt" in p for p in paths)

    def test_idempotent_second_run(self, ctx, cfg):
        stage = IngestStage()
        stage.execute(ctx)
        ctx.conn.commit()

        # Second run on fresh context (same DB)
        conn2 = open_db(cfg.db_path)
        ctx2 = RunContext.new(cfg, conn2, dry_run=False)
        r2 = stage.execute(ctx2)

        assert r2.files_changed == 0      # nothing new
        assert r2.files_skipped == 3      # all known
        assert get_archive_count(conn2) == 3

    def test_status_is_pending(self, ctx):
        IngestStage().execute(ctx)
        rows = ctx.conn.execute("SELECT status FROM archive").fetchall()
        for row in rows:
            assert row["status"] == "PENDING"

    def test_events_logged(self, ctx):
        IngestStage().execute(ctx)
        events = ctx.conn.execute(
            "SELECT event_type FROM events WHERE event_type='INGEST'"
        ).fetchall()
        assert len(events) == 3


# ── IngestStage.dry_run() ─────────────────────────────────────────────────────

class TestIngestDryRun:
    def test_dry_run_no_db_changes(self, ctx_dry):
        stage = IngestStage()
        result = stage.execute(ctx_dry)

        assert result.dry_run is True
        assert result.files_changed == 3   # would change 3
        assert get_archive_count(ctx_dry.conn) == 0  # DB unchanged

    def test_dry_run_notes_list_files(self, ctx_dry):
        stage = IngestStage()
        result = stage.execute(ctx_dry)

        all_notes = "\n".join(result.notes)
        assert "Would ingest" in all_notes or "new file" in all_notes.lower()

    def test_dry_run_empty_inbox(self, tmp_path):
        """Empty inbox → dry_run reports nothing to do."""
        # Use a completely isolated subdirectory, not shared with vault fixture
        root = tmp_path / "empty_vault"
        root.mkdir()
        cfg2 = MusicConfig(
            vault_root=root,
            inbox=root / "INBOX",
            staging=root / "STAGING",
            quarantine=root / "QUARANTINE",
            runs_root=root / "RUNS",
            meta_dir=root / "MetaData",
            alac_library=root / "ALAC-Library",
            db_path=root / "musaeus.db",
        )
        (root / "INBOX").mkdir()
        conn = open_db(cfg2.db_path)
        ctx = RunContext.new(cfg2, conn, dry_run=True)
        result = IngestStage().execute(ctx)

        assert result.files_changed == 0
        assert any("No new" in n or "0" in n for n in result.notes)
