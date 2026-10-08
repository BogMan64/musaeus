"""Review of #86, finding 14 (2026-10-07): Finalize's verify_effect sampled the
five newest CATALOGUED rows, not the files this run moved, so it could report
"verified" without looking at one of them. It now checks this run's moves.
"""

from __future__ import annotations

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.base import NO_VERIFICATION
from musaeus.stages.finalize import FinalizeStage


@pytest.fixture
def ctx(tmp_path):
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q", runs_root=tmp_path / "RUNS", meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library", alac_archive=tmp_path / "ALAC-Archival",
        db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    cfg.ensure_dirs()
    c = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    other = tmp_path / "ALAC-Archival" / "older.m4a"  # an earlier batch, on disk
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(b"x")
    upsert_archive(c.conn, {"file_path": str(other), "status": "CATALOGUED"})
    c.conn.commit()
    return c


def test_a_claimed_move_with_no_record_is_a_problem(ctx):
    stage = FinalizeStage()
    result = stage._make_result(dry_run=False)
    result.files_changed = 3  # claims three moves; this run recorded none

    assert stage.verify_effect(ctx, result) not in ([], NO_VERIFICATION)


def test_a_moved_file_that_is_not_there_is_a_problem(ctx):
    target = ctx.config.alac_archive / "A" / "song.m4a"  # never written
    upsert_archive(ctx.conn, {"file_path": str(target), "status": "CATALOGUED"})
    ctx.log_event("FINALIZE_MOVE", file_path=str(target), old_value="/staging/song.m4a",
                  new_value=str(target), stage="finalize")  # fmt: skip
    ctx.conn.commit()
    stage = FinalizeStage()
    result = stage._make_result(dry_run=False)
    result.files_changed = 1

    assert stage.verify_effect(ctx, result) not in ([], NO_VERIFICATION)


def test_this_runs_moves_on_disk_pass(ctx):
    target = ctx.config.alac_archive / "A" / "song.m4a"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"song")
    upsert_archive(ctx.conn, {"file_path": str(target), "status": "CATALOGUED"})
    ctx.log_event("FINALIZE_MOVE", file_path=str(target), old_value="/staging/song.m4a",
                  new_value=str(target), stage="finalize")  # fmt: skip
    ctx.conn.commit()
    stage = FinalizeStage()
    result = stage._make_result(dry_run=False)
    result.files_changed = 1

    assert stage.verify_effect(ctx, result) == []
