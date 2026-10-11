"""Review of slice B (#145), finding 6: Sentinel's GHOST recovery never
happened for a real ghost. GhostStage changes only the status, so a ghost
keeps its hash, and Sentinel chose only PENDING or NULL-hash rows. A master
whose file came back stayed GHOST, out of every stage and edition."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.ghost import GhostStage
from musaeus.stages.sentinel import SentinelStage

HASH = "a" * 64


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        db_path=tmp_path / "musaeus.db",
    )
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _ghosted_master(ctx, tmp_path) -> Path:
    """A filed master whose drive went away, as GhostStage marks it."""
    master = tmp_path / "Libraries" / "ALAC-Archival" / "Rock" / "A" / "B" / "song.m4a"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"AUDIO")
    upsert_archive(
        ctx.conn,
        {"file_path": str(master), "status": "CATALOGUED", "audio_hash": HASH, "full_hash": "f"},
    )
    ctx.conn.commit()
    away = master.with_suffix(".away")
    master.rename(away)
    GhostStage().execute(ctx)
    away.rename(master)  # the drive is back
    return master


def _status(ctx, path):
    return ctx.conn.execute(
        "SELECT status FROM archive WHERE file_path = ?", (str(path),)
    ).fetchone()["status"]


@patch("musaeus.stages.sentinel.audio_hash_checked")
@patch("musaeus.stages.sentinel.file_hash")
def test_a_master_whose_file_returns_is_catalogued_again(mock_fh, mock_ah, ctx, tmp_path):
    master = _ghosted_master(ctx, tmp_path)
    assert _status(ctx, master) == "GHOST"
    mock_fh.return_value = "f"
    mock_ah.return_value = (HASH, None, "")
    SentinelStage().execute(ctx)
    assert _status(ctx, master) == "CATALOGUED", "the returning master stayed GHOST"


@patch("musaeus.stages.sentinel.audio_hash_checked")
@patch("musaeus.stages.sentinel.file_hash")
def test_different_audio_at_the_path_starts_again(mock_fh, mock_ah, ctx, tmp_path):
    master = _ghosted_master(ctx, tmp_path)
    mock_fh.return_value = "g"
    mock_ah.return_value = ("b" * 64, None, "")
    SentinelStage().execute(ctx)
    assert _status(ctx, master) == "HASHED"


@patch("musaeus.stages.sentinel.audio_hash_checked")
@patch("musaeus.stages.sentinel.file_hash")
def test_a_ghost_still_missing_is_left_alone(mock_fh, mock_ah, ctx, tmp_path):
    master = _ghosted_master(ctx, tmp_path)
    master.unlink()
    result = SentinelStage().execute(ctx)
    assert _status(ctx, master) == "GHOST"
    assert not result.errors, "a known ghost failed the run again"


@patch("musaeus.stages.sentinel.audio_hash_checked")
@patch("musaeus.stages.sentinel.file_hash")
def test_a_returned_ghost_that_does_not_decode_does_not_fail_every_run(
    mock_fh, mock_ah, ctx, tmp_path
):
    master = _ghosted_master(ctx, tmp_path)
    mock_fh.return_value = "f"
    mock_ah.return_value = (None, "ffmpeg exited 1: Invalid data found", "")
    for _ in range(2):
        result = SentinelStage().execute(ctx)
        assert not result.errors, "a known ghost failed Sentinel"
    assert _status(ctx, master) == "GHOST"
