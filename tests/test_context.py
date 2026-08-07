"""
Tests for musaeus.context — RunContext, StageResult.
"""

from pathlib import Path

import pytest
from musaeus.config import MusicConfig
from musaeus.context import RunContext, StageResult
from musaeus.db import open_db


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
    return MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        db_path=tmp_path / "musaeus.db",
        aac_car_root=tmp_path / "RUNS" / "AAC-Car",
        aac_car_masked_root=tmp_path / "RUNS" / "AAC-Car-Masked",
        noise_dir=tmp_path / "RUNS" / "Noise",
    )


@pytest.fixture
def conn(cfg):
    c = open_db(cfg.db_path)
    yield c
    try:
        c.close()
    except Exception:
        pass


# ── StageResult ───────────────────────────────────────────────────────────────

class TestStageResult:
    def test_summarise_success(self):
        r = StageResult("ingest", success=True, files_processed=10, files_changed=8)
        s = r.summarise()
        assert "ingest" in s
        assert "OK" in s
        assert "processed=10" in s

    def test_summarise_failure(self):
        r = StageResult("sentinel", success=False)
        assert "FAILED" in r.summarise()

    def test_summarise_dry_run(self):
        r = StageResult("scholar", success=True, dry_run=True)
        assert "DRY RUN" in r.summarise()


# ── RunContext.new() ──────────────────────────────────────────────────────────

class TestRunContextNew:
    def test_creates_run_id(self, cfg, conn):
        ctx = RunContext.new(cfg, conn)
        assert ctx.run_id.startswith("run_")

    def test_custom_run_id(self, cfg, conn):
        ctx = RunContext.new(cfg, conn, run_id="my_custom_id")
        assert ctx.run_id == "my_custom_id"

    def test_logs_run_start(self, cfg, conn):
        ctx = RunContext.new(cfg, conn)
        row = conn.execute(
            "SELECT event_type FROM events WHERE run_id=?",
            (ctx.run_id,),
        ).fetchone()
        assert row["event_type"] == "RUN_START"

    def test_dry_run_flag(self, cfg, conn):
        """P0-05: RunContext.new() rejects dry_run=True; use the pure planner instead."""
        with pytest.raises(ValueError, match="RunContext is execution-only"):
            RunContext.new(cfg, conn, dry_run=True)

    def test_default_not_dry_run(self, cfg, conn):
        ctx = RunContext.new(cfg, conn)
        assert ctx.dry_run is False


# ── RunContext accessors ──────────────────────────────────────────────────────

class TestRunContextAccessors:
    def test_path_accessors(self, cfg, conn):
        ctx = RunContext.new(cfg, conn)
        assert ctx.vault_root == cfg.vault_root
        assert ctx.inbox == cfg.inbox
        assert ctx.staging == cfg.staging
        assert ctx.quarantine == cfg.quarantine

    def test_stash_set_get(self, cfg, conn):
        ctx = RunContext.new(cfg, conn)
        ctx.set("my_key", [1, 2, 3])
        assert ctx.get("my_key") == [1, 2, 3]

    def test_stash_default(self, cfg, conn):
        ctx = RunContext.new(cfg, conn)
        assert ctx.get("missing", "fallback") == "fallback"

    def test_run_dir(self, cfg, conn):
        ctx = RunContext.new(cfg, conn)
        assert ctx.run_dir == cfg.runs_root / ctx.run_id

    def test_ensure_run_dir_creates(self, cfg, conn):
        ctx = RunContext.new(cfg, conn)
        d = ctx.ensure_run_dir()
        assert d.exists()
        assert d.is_dir()


# ── RunContext.record_stage() ─────────────────────────────────────────────────

class TestRunContextRecordStage:
    def test_records_result(self, cfg, conn):
        ctx = RunContext.new(cfg, conn)
        r = StageResult("ingest", success=True, files_changed=5)
        ctx.record_stage(r)
        assert len(ctx.stage_results) == 1
        assert ctx.stage_results[0].files_changed == 5

    def test_logs_stage_complete_event(self, cfg, conn):
        ctx = RunContext.new(cfg, conn)
        ctx.record_stage(StageResult("sentinel", success=True))
        events = conn.execute(
            "SELECT event_type, stage FROM events WHERE event_type='STAGE_COMPLETE'"
        ).fetchall()
        assert len(events) == 1
        assert events[0]["stage"] == "sentinel"


# ── RunContext.finish() ───────────────────────────────────────────────────────

class TestRunContextFinish:
    def test_logs_run_end(self, cfg, tmp_path):
        db_path = tmp_path / "finish_test.db"
        conn = open_db(db_path)
        ctx = RunContext.new(cfg, conn)
        run_id = ctx.run_id
        ctx.finish()

        # Re-open to verify event was written
        conn2 = open_db(db_path)
        row = conn2.execute(
            "SELECT event_type FROM events WHERE run_id=? AND event_type='RUN_END'",
            (run_id,),
        ).fetchone()
        assert row is not None
        conn2.close()
