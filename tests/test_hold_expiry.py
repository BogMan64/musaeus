"""
Tests for HoldExpiryStage.

Grey, 2026-11-05: what REVIEW and QUARANTINE hold is deleted after 30 days,
with nobody checking it by hand. So what matters most here is what the stage
must refuse, because no person is left to catch it: a file too young, the last
copy of a song, a song Grey put on the keep list, a file nothing vouches for.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import (
    ensure_deny_list,
    lookup_denied_hash,
    open_db,
    open_hash_index,
    upsert_archive,
)
from musaeus.stages import hold_expiry
from musaeus.stages.hold_expiry import HOLD_DAYS, HoldExpiryStage


@pytest.fixture
def vault(tmp_path: Path) -> MusicConfig:
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )
    for d in (cfg.inbox, cfg.quarantine, cfg.meta_dir, cfg.hash_index_path.parent):
        d.mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def ctx(vault) -> RunContext:
    return RunContext.new(vault, open_db(vault.db_path), dry_run=False)


@pytest.fixture
def past_the_hold(monkeypatch):
    """ctime cannot be set, so the clock moves instead."""
    later = time.time() + (HOLD_DAYS + 1) * 86400
    monkeypatch.setattr(hold_expiry.time, "time", lambda: later)


def _held(ctx, folder: Path, name: str, status: str, audio_hash: str, **fields) -> Path:
    path = folder / "2026-11-05" / "Some Artist" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio")
    upsert_archive(
        ctx.conn,
        {"file_path": str(path), "status": status, "audio_hash": audio_hash, **fields},
    )
    ctx.conn.commit()
    return path


def _keeper(ctx, loser_hash: str, keeper_hash: str, keeper_status: str = "CATALOGUED") -> Path:
    path = ctx.config.alac_library / "keeper.m4a"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio")
    upsert_archive(
        ctx.conn,
        {"file_path": str(path), "status": keeper_status, "audio_hash": keeper_hash},
    )
    ctx.conn.executemany(
        "INSERT INTO duplicates (group_id, file_path, status, audio_hash) VALUES ('g', ?, ?, ?)",
        [("/inbox/keeper.flac", "keep", keeper_hash), ("/inbox/loser.flac", "archive", loser_hash)],
    )
    ctx.conn.commit()
    return path


def _denied(ctx, audio_hash: str) -> bool:
    conn = open_hash_index(ctx.config.hash_index_path)
    ensure_deny_list(conn)
    found = lookup_denied_hash(conn, audio_hash) is not None
    conn.close()
    return found


def _row(ctx, path: Path):
    return ctx.conn.execute("SELECT 1 FROM archive WHERE file_path = ?", (str(path),)).fetchone()


def test_a_file_past_the_hold_goes_with_its_row_and_its_records_stay(ctx, past_the_hold):
    cfg = ctx.config
    song = _held(ctx, cfg.tribute_review_dir, "Hey Jude.flac", "TRIBUTE_REVIEW", "h1")
    manifest = song.parents[1] / "tribute_manifest.csv"
    manifest.write_text("source,destination\n")

    result = HoldExpiryStage().run(ctx)

    assert not song.exists() and _row(ctx, song) is None
    assert _denied(ctx, "h1")
    assert manifest.exists()
    assert not song.parent.exists(), "an emptied artist folder goes too"
    assert result.files_changed == 1
    assert HoldExpiryStage().verify_effect(ctx, result) == []


@pytest.mark.parametrize(
    ("place", "status"),
    [
        (lambda c: c.dupes_review_dir, "DUPE_REVIEW"),
        (lambda c: c.tribute_review_dir, "TRIBUTE_REVIEW"),
        (lambda c: c.quarantine / "denied", "QUARANTINED"),
        (lambda c: c.quarantine / "corrupted", "QUARANTINED"),
    ],
    ids=["dupes", "tribute", "denied", "corrupted"],
)
def test_every_held_folder_is_covered_and_nothing_goes_early(ctx, monkeypatch, place, status):
    song = _held(ctx, place(ctx.config), "song.flac", status, "h1")
    _keeper(ctx, "h1", "k1")

    HoldExpiryStage().run(ctx)
    assert song.exists(), "a file inside the hold stays"

    later = time.time() + (HOLD_DAYS + 1) * 86400
    monkeypatch.setattr(hold_expiry.time, "time", lambda: later)
    HoldExpiryStage().run(ctx)
    assert not song.exists()


def test_a_damaged_file_is_not_denied_so_its_replacement_can_arrive(ctx, past_the_hold):
    song = _held(ctx, ctx.config.quarantine / "corrupted", "x.flac", "QUARANTINED", "h1")

    HoldExpiryStage().run(ctx)

    assert not song.exists()
    assert not _denied(ctx, "h1")


@pytest.mark.parametrize(
    "keeper_status",
    ["DUPE_REVIEW", "GHOST"],
    ids=["keeper set aside too", "keeper gone"],
)
def test_a_dupe_loser_whose_keeper_left_the_library_is_the_last_copy(
    ctx, past_the_hold, keeper_status
):
    song = _held(ctx, ctx.config.dupes_review_dir, "song.flac", "DUPE_REVIEW", "h1")
    _keeper(ctx, "h1", "k1", keeper_status)

    result = HoldExpiryStage().run(ctx)

    assert song.exists() and _row(ctx, song)
    assert result.files_skipped == 1
    assert any("left the library" in n for n in result.notes)


def test_a_keeper_row_with_no_file_does_not_prove_a_copy_survives(ctx, past_the_hold):
    """Audit only reports a CATALOGUED row whose file has gone (CodeRabbit, #286)."""
    song = _held(ctx, ctx.config.dupes_review_dir, "song.flac", "DUPE_REVIEW", "h1")
    _keeper(ctx, "h1", "k1").unlink()

    HoldExpiryStage().run(ctx)

    assert song.exists() and _row(ctx, song)


def test_a_dupe_loser_goes_when_its_keeper_has_moved_since(ctx, past_the_hold):
    """`duplicates` still has the keeper's INBOX path; only its audio is current."""
    song = _held(ctx, ctx.config.dupes_review_dir, "song.flac", "DUPE_REVIEW", "h1")
    _keeper(ctx, "h1", "k1")

    HoldExpiryStage().run(ctx)

    assert not song.exists()


def test_audio_another_row_carries_is_not_denied(ctx, past_the_hold):
    """An exact copy shares its audio with the survivor; denying it would
    refuse the survivor on the next rebuild."""
    song = _held(ctx, ctx.config.dupes_review_dir, "song.flac", "DUPE_REVIEW", "shared")
    _keeper(ctx, "shared", "shared")

    HoldExpiryStage().run(ctx)

    assert not song.exists()
    assert not _denied(ctx, "shared")


def test_a_song_on_the_keep_list_stays(ctx, past_the_hold):
    (ctx.config.meta_dir / "tribute_keep.tsv").write_text("Randy Crawford\tStreet Life\n")
    song = _held(
        ctx,
        ctx.config.tribute_review_dir,
        "Street Life.flac",
        "TRIBUTE_REVIEW",
        "h1",
        artist="Randy Crawford",
        title="Street Life (Edit)",
    )

    HoldExpiryStage().run(ctx)

    assert song.exists()


def test_a_file_no_held_row_vouches_for_stays(ctx, past_the_hold):
    folder = ctx.config.dupes_review_dir
    stray = folder / "stray.flac"
    folder.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"audio")
    live = _held(ctx, folder, "live.flac", "CATALOGUED", "h1")

    result = HoldExpiryStage().run(ctx)

    assert stray.exists() and live.exists()
    assert result.files_skipped == 2


def test_a_preview_deletes_nothing(ctx, past_the_hold):
    song = _held(ctx, ctx.config.tribute_review_dir, "song.flac", "TRIBUTE_REVIEW", "h1")

    result = HoldExpiryStage().dry_run(ctx)

    assert song.exists() and _row(ctx, song)
    assert not ctx.config.hash_index_path.exists(), "a preview creates no ledger"
    assert result.files_changed == 1


def test_a_failed_delete_leaves_no_denial_for_the_next_file_to_commit(
    ctx, past_the_hold, monkeypatch
):
    folder = ctx.config.tribute_review_dir
    stuck = _held(ctx, folder, "a.flac", "TRIBUTE_REVIEW", "h1")
    goes = _held(ctx, folder, "b.flac", "TRIBUTE_REVIEW", "h2")
    real_unlink = Path.unlink

    def refuse_one(self):
        if self == stuck:
            raise PermissionError("read-only")
        real_unlink(self)

    monkeypatch.setattr(Path, "unlink", refuse_one)
    HoldExpiryStage().run(ctx)

    assert stuck.exists() and not goes.exists()
    assert not _denied(ctx, "h1")
    assert _denied(ctx, "h2")


def test_a_file_that_will_not_delete_keeps_its_row(ctx, past_the_hold, monkeypatch):
    song = _held(ctx, ctx.config.tribute_review_dir, "song.flac", "TRIBUTE_REVIEW", "h1")

    def refuse(self):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "unlink", refuse)
    result = HoldExpiryStage().run(ctx)

    assert _row(ctx, song)
    assert not _denied(ctx, "h1"), "nothing is denied for a file still here"
    assert not ctx.conn.execute("SELECT 1 FROM events WHERE event_type = 'HOLD_EXPIRED'").fetchone()
    assert result.files_errored == 1 and result.files_changed == 0
    assert not result.success
