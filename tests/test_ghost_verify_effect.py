"""A row marked GHOST must really have no file behind it.

GhostStage rewrites status wholesale from a single existence test per row --
3,143 rows in one pass on 2026-09-05. If that test is ever wrong (a mount not
ready, a directory it cannot read, a path built before a rename landed) it
buries a live library under a status every later stage skips, and reports OK
while doing it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.ghost import GhostStage


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
    return MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )


@pytest.fixture
def ctx(cfg: MusicConfig) -> RunContext:
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _ghosted(ctx, path: Path, on_disk: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if on_disk:
        path.write_bytes(b"x")
    upsert_archive(ctx.conn, {"file_path": str(path), "status": "GHOST"})
    ctx.log_event("GHOST_FOUND", file_path=str(path), stage="ghost")
    ctx.conn.commit()


class TestGhostVerifyEffect:
    def test_a_genuinely_missing_file_passes(self, ctx, tmp_path):
        _ghosted(ctx, tmp_path / "gone.m4a", on_disk=False)
        assert GhostStage().verify_effect(ctx, MagicMock(files_changed=1)) == []

    def test_a_row_ghosted_while_its_file_exists_is_caught(self, ctx, tmp_path):
        """The failure that matters: a live file buried under GHOST."""
        _ghosted(ctx, tmp_path / "alive.m4a", on_disk=True)
        problems = GhostStage().verify_effect(ctx, MagicMock(files_changed=1))
        assert problems, "a GHOST row whose file is present must not pass"
        assert "alive.m4a" in problems[0]

    def test_nothing_ghosted_this_run_is_nothing_to_verify(self, ctx):
        assert GhostStage().verify_effect(ctx, MagicMock(files_changed=0)) == []

    def test_only_this_run_is_sampled(self, ctx, tmp_path):
        """A GHOST row from an earlier run is not this run's claim -- and its
        file may legitimately have been restored since."""
        p = tmp_path / "old.m4a"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        upsert_archive(ctx.conn, {"file_path": str(p), "status": "GHOST"})
        ctx.conn.execute(
            "INSERT INTO events (run_id, event_type, file_path, stage) VALUES (?,?,?,?)",
            ("some_earlier_run", "GHOST_FOUND", str(p), "ghost"),
        )
        ctx.conn.commit()
        assert GhostStage().verify_effect(ctx, MagicMock(files_changed=1)) == []
