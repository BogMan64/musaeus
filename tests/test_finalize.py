"""
Tests for FinalizeStage — move canonicalized files from INBOX into the
canonical vault_root/ALAC-Library.

Uses real files on disk (not mocked): this stage's whole purpose is a
real filesystem move plus a real persistent hash-index write, and a
mock would hide exactly the kind of bug a real move can produce (e.g.
organize.py's row["rowid"]/unique_path bugs, found and fixed earlier
this session, only surfaced under a real live run).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, open_hash_index, upsert_archive
from musaeus.stages.finalize import FinalizeStage


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


def _make_canonicalized_track(
    ctx: RunContext,
    relpath: str,
    artist: str,
    album: str,
    title: str,
    audio_hash: str = "deadbeef",
) -> Path:
    """Create a real file under INBOX, registered as already-canonicalized
    (canonicalized_at set, finalized_at NULL) -- the state Finalize expects
    to find its input in."""
    path = ctx.inbox / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"FAKE CANONICAL AUDIO DATA")
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(path),
            "status": "CATALOGUED",
            "artist": artist,
            "album": album,
            "title": title,
            "audio_hash": audio_hash,
        },
    )
    ctx.conn.execute(
        "UPDATE archive SET canonicalized_at = datetime('now'), canon_action = 'PASSTHROUGH' "
        "WHERE file_path = ?",
        (str(path),),
    )
    ctx.conn.commit()
    return path


# ── Live run ──────────────────────────────────────────────────────────────────

class TestFinalizeRunLive:
    def test_moves_file_into_alac_library(self, ctx):
        track = _make_canonicalized_track(ctx, "flat.m4a", "Test Artist", "Test Album", "Song One")

        result = FinalizeStage().execute(ctx)

        assert result.success is True
        assert result.files_changed == 1
        assert not track.exists()

        expected = ctx.alac_library / "Test Artist" / "Test Album" / "Test Artist - Song One.m4a"
        assert expected.exists()
        assert expected.read_bytes() == b"FAKE CANONICAL AUDIO DATA"

        row = ctx.conn.execute(
            "SELECT file_path, finalized_at FROM archive WHERE title='Song One'"
        ).fetchone()
        assert row["file_path"] == str(expected)
        assert row["finalized_at"] is not None

    def test_multiple_files_same_run(self, ctx):
        _make_canonicalized_track(ctx, "a.m4a", "Artist A", "Album A", "Title A", "hash_a")
        _make_canonicalized_track(ctx, "b.m4a", "Artist B", "Album B", "Title B", "hash_b")

        result = FinalizeStage().execute(ctx)

        assert result.files_changed == 2
        assert (ctx.alac_library / "Artist A" / "Album A" / "Artist A - Title A.m4a").exists()
        assert (ctx.alac_library / "Artist B" / "Album B" / "Artist B - Title B.m4a").exists()

    def test_empty_inbox_dirs_cleaned_up(self, ctx):
        _make_canonicalized_track(ctx, "nested/deep/path/track.m4a", "Artist", "Album", "Title")

        FinalizeStage().execute(ctx)

        # The nested source directory tree should be gone -- nothing left
        # behind in INBOX once its only file has moved out.
        assert not (ctx.inbox / "nested").exists()
        assert ctx.inbox.exists()  # INBOX root itself is not removed

    def test_hash_recorded_in_persistent_index(self, ctx):
        _make_canonicalized_track(ctx, "track.m4a", "Artist", "Album", "Title", "myhash123")

        FinalizeStage().execute(ctx)

        hash_conn = open_hash_index(ctx.config.hash_index_path)
        rows = hash_conn.execute(
            "SELECT file_path FROM finalized_hashes WHERE audio_hash = ?",
            ("myhash123",),
        ).fetchall()
        hash_conn.close()

        assert len(rows) == 1
        expected = ctx.alac_library / "Artist" / "Album" / "Artist - Title.m4a"
        assert rows[0]["file_path"] == str(expected)

    def test_hash_index_survives_after_vault_db_would_be_wiped(self, ctx):
        """The whole point of the persistent index: it must be a real,
        independent file on disk, not just an in-memory or same-DB
        artifact that vanishes when musaeus.db is wiped."""
        _make_canonicalized_track(ctx, "track.m4a", "Artist", "Album", "Title", "survives123")
        FinalizeStage().execute(ctx)

        # Simulate the vault DB being wiped between batches.
        ctx.conn.close()
        ctx.config.db_path.unlink()

        # The hash index file must still exist and be queryable on its own.
        assert ctx.config.hash_index_path.exists()
        hash_conn = open_hash_index(ctx.config.hash_index_path)
        rows = hash_conn.execute(
            "SELECT file_path FROM finalized_hashes WHERE audio_hash = ?",
            ("survives123",),
        ).fetchall()
        hash_conn.close()
        assert len(rows) == 1

    def test_missing_file_reported_not_crash(self, ctx):
        missing = ctx.inbox / "gone.m4a"
        upsert_archive(ctx.conn, {
            "file_path": str(missing), "status": "CATALOGUED",
            "artist": "A", "album": "B", "title": "C",
        })
        ctx.conn.execute(
            "UPDATE archive SET canonicalized_at = datetime('now') WHERE file_path = ?",
            (str(missing),),
        )
        ctx.conn.commit()

        result = FinalizeStage().execute(ctx)

        assert result.files_errored == 1
        assert any("missing" in e.lower() for e in result.errors)

    def test_uncanonicalized_file_not_finalized(self, ctx):
        """A CATALOGUED row that Canonicalize hasn't touched yet
        (canonicalized_at IS NULL) must not be finalized."""
        path = ctx.inbox / "not_ready.m4a"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"DATA")
        upsert_archive(ctx.conn, {
            "file_path": str(path), "status": "CATALOGUED",
            "artist": "A", "album": "B", "title": "C",
        })
        ctx.conn.commit()

        result = FinalizeStage().execute(ctx)

        assert result.files_processed == 0
        assert path.exists()  # untouched


class TestFinalizeIdempotency:
    def test_already_finalized_skipped_on_rerun(self, ctx):
        _make_canonicalized_track(ctx, "track.m4a", "Artist", "Album", "Title")

        first = FinalizeStage().execute(ctx)
        assert first.files_changed == 1

        second = FinalizeStage().execute(ctx)
        assert second.files_processed == 0

    def test_force_on_already_finalized_does_not_duplicate(self, ctx):
        """--force on a file already at its correct ALAC-Library location
        must recognise that and skip, not create a ' (2)' duplicate --
        this is the same self-collision bug organize.py's unique_path()
        had, guarded against here the same way."""
        _make_canonicalized_track(ctx, "track.m4a", "Artist", "Album", "Title")
        FinalizeStage().execute(ctx)

        ctx.set("finalize_force", True)
        second = FinalizeStage().execute(ctx)

        assert second.files_errored == 0
        matches = list((ctx.alac_library / "Artist" / "Album").glob("*.m4a"))
        assert len(matches) == 1  # no " (2)" sibling created


# ── Dry run ───────────────────────────────────────────────────────────────────

class TestFinalizeDryRun:
    def test_dry_run_makes_no_changes(self, ctx_dry):
        track = _make_canonicalized_track(ctx_dry, "track.m4a", "Artist", "Album", "Title")

        result = FinalizeStage().execute(ctx_dry)

        assert result.dry_run is True
        assert track.exists()
        expected = ctx_dry.alac_library / "Artist" / "Album" / "Artist - Title.m4a"
        assert not expected.exists()

        row = ctx_dry.conn.execute("SELECT finalized_at FROM archive").fetchone()
        assert row["finalized_at"] is None
