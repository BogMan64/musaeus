"""Review of #86, finding 11 (2026-10-07): a Finalize killed after the copy
landed in ALAC-Archival, but before the archive row was updated, left that copy
untracked; the re-run found the path taken and made a "(2)" beside it. The
re-run now adopts its own copy when the bytes are identical and no row claims
it; anything else at the path is still never overwritten.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.finalize import FinalizeStage


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv(
        FinalizeStage.CHECKPOINT_ENV, "0"
    )  # the plain path; the boundary's undo is #100
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
        alac_archive=tmp_path / "ALAC-Archival", db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    cfg.ensure_dirs()
    c = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    c.set("finalize_batch_date", "2026-01-15")
    return c


def _staged(ctx, data: bytes) -> Path:
    path = ctx.staging / "Bob Seger - Night Moves.m4a"
    path.write_bytes(data)
    upsert_archive(ctx.conn, {"file_path": str(path), "status": "CATALOGUED", "artist": "Bob Seger",
                              "album": "Night Moves", "title": "Night Moves", "audio_hash": "cafe"})  # fmt: skip
    ctx.conn.execute("UPDATE archive SET canonicalized_at = datetime('now'), "
                     "canon_action = 'CONVERTED' WHERE file_path = ?", (str(path),))  # fmt: skip
    ctx.conn.commit()
    return path


def _row_path(ctx) -> str:
    return ctx.conn.execute("SELECT file_path FROM archive").fetchone()[0]


def test_the_rerun_adopts_its_own_copy(ctx):
    source = _staged(ctx, b"the song")
    target = FinalizeStage()._target_path(ctx, {"id": 1, "artist": "Bob Seger", "album": "Night Moves",
                                               "title": "Night Moves", "genre": None}, source)  # fmt: skip
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"the song")  # the copy the killed run left behind

    FinalizeStage().run(ctx)

    assert _row_path(ctx) == str(target)
    assert not list(ctx.config.alac_archive.rglob("*(2)*")), 'a "(2)" second copy was made'
    assert not source.exists()


def test_a_different_file_at_the_path_is_left_alone(ctx):
    source = _staged(ctx, b"the song")
    target = FinalizeStage()._target_path(ctx, {"id": 1, "artist": "Bob Seger", "album": "Night Moves",
                                               "title": "Night Moves", "genre": None}, source)  # fmt: skip
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"another!")  # same size, different song

    FinalizeStage().run(ctx)

    assert target.read_bytes() == b"another!"
    assert _row_path(ctx) != str(target)
