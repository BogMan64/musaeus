"""
Tests for BitRotStage — re-hash CATALOGUED files against archive.full_hash
to catch silent bit rot. Standalone stage, not part of DEFAULT_PIPELINE.

Uses real files on disk (not mocked): this stage's whole purpose is a
real re-hash of real bytes, and a mock would hide exactly the kind of
bug a real hash comparison can catch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.hasher import file_hash
from musaeus.stages.bitrot import BitRotStage


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


def _make_row(ctx: RunContext, relpath: str, content: bytes) -> Path:
    path = ctx.alac_library / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(path),
            "status": "CATALOGUED",
            "full_hash": file_hash(path),
        },
    )
    ctx.conn.commit()
    return path


class TestBitRotDetection:
    def test_unchanged_file_reports_ok(self, ctx):
        _make_row(ctx, "Artist/Album/track.m4a", b"ORIGINAL CONTENT")

        result = BitRotStage().run(ctx)

        assert result.success is True
        assert any("ok: 1" in n for n in result.notes)
        row = ctx.conn.execute("SELECT bitrot_ok, bitrot_checked_at FROM archive").fetchone()
        assert row["bitrot_ok"] == 1
        assert row["bitrot_checked_at"] is not None

    def test_changed_bytes_detected_as_corrupt(self, ctx):
        """The core case this stage exists for: bytes silently change
        after the baseline hash was recorded."""
        path = _make_row(ctx, "Artist/Album/track.m4a", b"ORIGINAL CONTENT")
        path.write_bytes(b"CORRUPTED!!CONTENT")  # same length, different bytes

        result = BitRotStage().run(ctx)

        assert result.success is False
        assert any("corrupt (hash mismatch): 1" in n for n in result.notes)
        row = ctx.conn.execute("SELECT bitrot_ok FROM archive").fetchone()
        assert row["bitrot_ok"] == 0

        events = ctx.conn.execute(
            "SELECT event_type FROM events WHERE event_type='BITROT_DETECTED'"
        ).fetchall()
        assert len(events) == 1

    def test_stored_full_hash_never_overwritten_on_mismatch(self, ctx):
        """Overwriting full_hash with the corrupted file's new hash would
        silently erase the ability to detect the same corruption again
        next run -- must never happen."""
        path = _make_row(ctx, "Artist/Album/track.m4a", b"ORIGINAL CONTENT")
        original_hash = ctx.conn.execute("SELECT full_hash FROM archive").fetchone()[0]
        path.write_bytes(b"CORRUPTED!!CONTENT")

        BitRotStage().run(ctx)

        row = ctx.conn.execute("SELECT full_hash FROM archive").fetchone()
        assert row["full_hash"] == original_hash

    def test_missing_file_skipped_not_flagged_corrupt(self, ctx):
        """A missing file is GhostStage's job to record -- must not
        masquerade as a bit-rot finding."""
        path = _make_row(ctx, "Artist/Album/track.m4a", b"ORIGINAL CONTENT")
        path.unlink()

        result = BitRotStage().run(ctx)

        assert result.files_skipped == 1
        assert any("missing from disk: 1" in n for n in result.notes)
        assert not any("corrupt (hash mismatch): 1" in n for n in result.notes)

    def test_row_with_no_full_hash_not_a_candidate(self, ctx):
        path = ctx.alac_library / "Artist/Album/track.m4a"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"CONTENT")
        upsert_archive(
            ctx.conn, {"file_path": str(path), "status": "CATALOGUED", "full_hash": None}
        )
        ctx.conn.commit()

        result = BitRotStage().run(ctx)

        assert result.files_processed == 0

    def test_lufs_baked_row_excluded_from_candidates(self, ctx):
        """Real finding, confirmed live against the vault (2026-08-19):
        build_alac_library.py rewrites a file's bytes when baking LUFS but
        never refreshes full_hash, so a baked row's stored hash is stale
        relative to an intentional change, not evidence of corruption.
        Must not be treated as a candidate at all -- flagging it would be
        a guaranteed false positive, not a real bit-rot finding."""
        path = _make_row(ctx, "Artist/Album/track.m4a", b"ORIGINAL CONTENT")
        # Simulate what build_alac_library.py actually does: rewrite the
        # file's bytes (the bake) and set lufs_baked_at, WITHOUT touching
        # full_hash -- confirmed live to be exactly what happens today.
        path.write_bytes(b"BAKED CONTENT, DIFFERENT BYTES")
        ctx.conn.execute(
            "UPDATE archive SET lufs_baked_at = datetime('now') WHERE file_path = ?",
            (str(path),),
        )
        ctx.conn.commit()

        result = BitRotStage().run(ctx)

        assert result.files_processed == 0
        assert any("nothing to do" in n for n in result.notes)

    def test_re_run_on_unchanged_library_still_checks_every_time(self, ctx):
        """Unlike every other resumable stage tonight, this one must NOT
        skip already-checked rows on a second run -- catching newly
        occurring corruption requires re-verifying every time."""
        _make_row(ctx, "Artist/Album/track.m4a", b"ORIGINAL CONTENT")

        first = BitRotStage().run(ctx)
        second = BitRotStage().run(ctx)

        assert first.files_processed == 1
        assert second.files_processed == 1

    def test_limit_caps_rows_checked(self, ctx):
        for i in range(5):
            _make_row(ctx, f"Artist/Album/track{i}.m4a", f"CONTENT {i}".encode())
        ctx.set("bitrot_limit", 2)

        result = BitRotStage().run(ctx)

        assert result.files_processed == 2

    def test_dry_run_makes_no_changes(self, ctx):
        _make_row(ctx, "Artist/Album/track.m4a", b"ORIGINAL CONTENT")

        result = BitRotStage().dry_run(ctx)

        assert result.files_processed == 1
        row = ctx.conn.execute("SELECT bitrot_checked_at FROM archive").fetchone()
        assert row["bitrot_checked_at"] is None
