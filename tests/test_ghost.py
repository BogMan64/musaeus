"""
Tests for GhostStage — marks archive entries whose files no longer exist on disk.
"""

from pathlib import Path

import pytest
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.ghost import GhostStage


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


def _insert_archive(ctx: RunContext, file_path: str, status: str = "CATALOGUED") -> None:
    upsert_archive(ctx.conn, {"file_path": file_path, "status": status})
    ctx.conn.commit()


# ── Validate ──────────────────────────────────────────────────────────────────

class TestGhostValidate:
    def test_validate_empty_archive(self, ctx):
        """Empty archive → validate passes (just logs info)."""
        GhostStage().validate(ctx)

    def test_validate_with_entries(self, ctx, tmp_path):
        _insert_archive(ctx, str(tmp_path / "x.flac"))
        GhostStage().validate(ctx)


# ── Dry run ───────────────────────────────────────────────────────────────────

class TestGhostDryRun:
    def test_dry_run_missing_file(self, ctx_dry, tmp_path):
        """Missing file reported but not marked as GHOST."""
        _insert_archive(ctx_dry, str(tmp_path / "gone.flac"))
        result = GhostStage().execute(ctx_dry)

        assert result.dry_run is True
        assert result.files_changed == 1
        assert any("ghost" in n.lower() or "Would mark" in n for n in result.notes)

        # DB should NOT be changed
        row = ctx_dry.conn.execute(
            "SELECT status FROM archive WHERE file_path=?",
            (str(tmp_path / "gone.flac"),),
        ).fetchone()
        assert row["status"] == "CATALOGUED"

    def test_dry_run_existing_file(self, ctx_dry, tmp_path):
        """Existing files not reported as ghosts."""
        track = tmp_path / "exists.flac"
        track.write_bytes(b"DATA")
        _insert_archive(ctx_dry, str(track))

        result = GhostStage().execute(ctx_dry)
        assert result.files_changed == 0
        assert any("No ghosts" in n for n in result.notes)

    def test_dry_run_mixed(self, ctx_dry, tmp_path):
        """Mix of existing and missing files."""
        exists = tmp_path / "exists.flac"
        exists.write_bytes(b"OK")
        _insert_archive(ctx_dry, str(exists))
        _insert_archive(ctx_dry, str(tmp_path / "gone.flac"))

        result = GhostStage().execute(ctx_dry)
        assert result.files_changed == 1  # only the missing one


# ── Run ───────────────────────────────────────────────────────────────────────

class TestGhostRun:
    def test_marks_missing_as_ghost(self, ctx, tmp_path):
        _insert_archive(ctx, str(tmp_path / "missing.flac"))
        result = GhostStage().execute(ctx)

        assert result.files_changed == 1
        row = ctx.conn.execute(
            "SELECT status FROM archive WHERE file_path=?",
            (str(tmp_path / "missing.flac"),),
        ).fetchone()
        assert row["status"] == "GHOST"

    def test_existing_file_unchanged(self, ctx, tmp_path):
        track = tmp_path / "present.flac"
        track.write_bytes(b"AUDIO DATA")
        _insert_archive(ctx, str(track))

        result = GhostStage().execute(ctx)
        assert result.files_changed == 0
        assert result.files_skipped == 1

        row = ctx.conn.execute(
            "SELECT status FROM archive WHERE file_path=?",
            (str(track),),
        ).fetchone()
        assert row["status"] == "CATALOGUED"

    def test_already_ghost_not_double_logged(self, ctx, tmp_path):
        """Files already GHOST should be skipped (not re-logged)."""
        _insert_archive(ctx, str(tmp_path / "old_ghost.flac"), status="GHOST")
        result = GhostStage().execute(ctx)

        assert result.files_changed == 0
        assert result.files_skipped == 1
        # No GHOST_FOUND event should be logged
        events = ctx.conn.execute(
            "SELECT * FROM events WHERE event_type='GHOST_FOUND'"
        ).fetchall()
        assert len(events) == 0

    def test_events_logged(self, ctx, tmp_path):
        _insert_archive(ctx, str(tmp_path / "vanished.flac"))
        GhostStage().execute(ctx)

        events = ctx.conn.execute(
            "SELECT * FROM events WHERE event_type='GHOST_FOUND'"
        ).fetchall()
        assert len(events) == 1
        assert events[0]["file_path"] == str(tmp_path / "vanished.flac")
        assert events[0]["new_value"] == "GHOST"

    def test_multiple_ghosts(self, ctx, tmp_path):
        for i in range(5):
            _insert_archive(ctx, str(tmp_path / f"missing_{i}.flac"))

        result = GhostStage().execute(ctx)
        assert result.files_changed == 5

    def test_empty_archive(self, ctx):
        """Empty archive → no changes."""
        result = GhostStage().execute(ctx)
        assert result.files_processed == 0
        assert result.files_changed == 0
