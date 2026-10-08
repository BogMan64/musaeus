"""Findings of the 2026-09-23 ultrareview (PR #21) still true on 2026-10-07, fixed.

R1: a CROSS_BATCH duplicate merged into a NEAR component was kept, and another song moved.
R4: verify_effect sampled whatever sat in review, and answered "nothing wrong" when it saw nothing.
R5: the "relocated" query compared a path with itself, so it never matched, and its result was
    never used anyway.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.dupe_resolver import DupeResolverStage


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    cfg.meta_dir.mkdir(parents=True)
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _row(ctx, path: Path, **kw) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"X" * kw.pop("size_bytes", 300))
    kw.setdefault("audio_hash", "pcm:" + path.name)
    upsert_archive(ctx.conn, {"file_path": str(path), "status": "CATALOGUED", **kw})
    ctx.conn.commit()
    return path


def _dup(ctx, gid, path, dtype, audio=None):
    ctx.conn.execute(
        "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, run_id, audio_hash) "
        "VALUES (?, ?, ?, 1.0, ?, ?)", (gid, str(path), dtype, ctx.run_id, audio or "pcm:" + Path(path).name))  # fmt: skip
    ctx.conn.commit()


def test_R1_an_incoming_cross_batch_duplicate_always_moves(ctx):
    x = _row(
        ctx,
        ctx.inbox / "incoming.flac",
        artist="A",
        album="B",
        title="T",
        codec="alac",
        bitrate=900_000,
    )
    y = _row(
        ctx,
        ctx.inbox / "other.m4a",
        artist="A",
        album="B",
        title="T (Live)",
        codec="aac",
        bitrate=256_000,
    )
    _dup(ctx, "crossdupe_aaaaaaaaaaaa", x, "CROSS_BATCH")  # x is already in the library
    _dup(ctx, "near_bbbbbbbb", x, "NEAR")
    _dup(ctx, "near_bbbbbbbb", y, "NEAR")
    DupeResolverStage().execute(ctx)
    assert not x.exists(), "a confirmed CROSS_BATCH duplicate must always move"
    assert y.exists(), "the other song is kept"


def test_R4_moves_claimed_but_none_recorded_is_a_problem(ctx):
    stage = DupeResolverStage()
    result = stage._make_result(dry_run=False)
    result.files_changed = 5
    assert stage.verify_effect(ctx, result) != []


def test_R4_a_run_that_moved_nothing_has_nothing_to_answer_for(ctx):
    stage = DupeResolverStage()
    assert stage.verify_effect(ctx, stage._make_result(dry_run=False)) == []


def test_R4_a_real_move_verifies(ctx):
    best = _row(
        ctx,
        ctx.inbox / "best.flac",
        artist="A",
        album="B",
        title="T",
        codec="alac",
        bitrate=900_000,
    )
    worse = _row(
        ctx, ctx.inbox / "worse.m4a", artist="A", album="B", title="T", codec="aac", bitrate=128_000
    )
    _dup(ctx, "near_cccccccc", best, "NEAR")
    _dup(ctx, "near_cccccccc", worse, "NEAR")
    stage = DupeResolverStage()
    result = stage.execute(ctx)
    assert result.files_changed == 1 and stage.verify_effect(ctx, result) == []


def test_R5_a_duplicate_another_stage_moved_is_recognised_by_its_recording(ctx):
    keep = _row(
        ctx,
        ctx.inbox / "keep.flac",
        artist="A",
        album="B",
        title="T",
        codec="alac",
        bitrate=900_000,
    )
    old = ctx.inbox / "old" / "song.m4a"  # where the group last saw it
    new = _row(ctx, ctx.inbox / "Composer" / "song.m4a", artist="A", album="B", title="T",
               codec="aac", bitrate=128_000, audio_hash="pcm:song")  # fmt: skip
    _dup(ctx, "near_dddddddd", keep, "NEAR")
    _dup(ctx, "near_dddddddd", old, "NEAR", audio="pcm:song")
    result = DupeResolverStage().execute(ctx)
    assert new.exists()
    assert any("relocated by another stage" in n for n in result.notes), result.notes
    assert result.files_errored == 0


def test_a_group_flagged_cross_batch_throughout_keeps_one_copy(ctx):
    """Review of #86, finding 3, a regression from the R1 fix: when every member
    of a component was CROSS_BATCH-flagged, R1 chose no keeper and moved them
    all -- library copies too, since CrossDupe can flag a file at a ledger path
    (finding 2). Never move a whole group: keep the ranked keeper."""
    a = _row(ctx, ctx.alac_library / "A" / "a.m4a", artist="A", album="B", title="T",
             codec="alac", bitrate=900_000)  # fmt: skip
    b = _row(ctx, ctx.alac_library / "A" / "b.m4a", artist="A", album="B", title="T",
             codec="aac", bitrate=256_000)  # fmt: skip
    for path, gid in ((a, "crossdupe_aaaaaaaaaaaa"), (b, "crossdupe_bbbbbbbbbbbb")):
        _dup(ctx, gid, path, "CROSS_BATCH")
    _dup(ctx, "near_cccccccc", a, "NEAR")
    _dup(ctx, "near_cccccccc", b, "NEAR")

    DupeResolverStage().execute(ctx)

    assert a.exists() or b.exists(), "every copy of the song was moved"
    assert a.exists() and not b.exists(), "the better copy stays"


def test_nothing_moves_when_the_keepers_file_is_missing(ctx):
    """Review of #86, finding 6: nobody checked the keeper's file exists. A
    keeper whose file is gone meant every real copy was moved out."""
    a = _row(ctx, ctx.alac_library / "A" / "a.m4a", artist="A", album="B", title="T",
             codec="alac", bitrate=900_000)  # fmt: skip
    b = _row(ctx, ctx.alac_library / "A" / "b.m4a", artist="A", album="B", title="T",
             codec="aac", bitrate=256_000)  # fmt: skip
    _dup(ctx, "near_dddddddd", a, "NEAR")
    _dup(ctx, "near_dddddddd", b, "NEAR")
    a.unlink()  # the better copy's file is gone; its row is not

    DupeResolverStage().execute(ctx)

    assert b.exists(), "the only remaining copy was moved out"
