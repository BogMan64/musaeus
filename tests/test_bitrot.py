"""
Tests for BitRotStage — verify ALAC_Archive content against a
directory-scan baseline (archive_tier_hashes) to catch silent bit rot.
Standalone stage, not part of DEFAULT_PIPELINE.

Second design (2026-08-19, same session): the first version compared
against archive.full_hash instead, but that goes stale for nearly every
finalized file (Canonicalize/Forge/Tagger all legitimately rewrite bytes
after Sentinel computes it). This version establishes its own fresh
baseline from ALAC_Archive's current state and is deliberately not tied
to archive.id/file_path at all -- directory-scan based, keyed by path,
matching ALAC_Archive's own "not DB-row-tracked" design.

Uses real files on disk (not mocked): this stage's whole purpose is a
real hash of real bytes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db
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


def _archive_file(ctx: RunContext, relpath: str, content: bytes) -> Path:
    path = ctx.config.alac_archive / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _baseline(ctx: RunContext, path: Path) -> None:
    ctx.conn.execute(
        "INSERT INTO archive_tier_hashes (path, sha256, size_bytes) VALUES (?, ?, ?)",
        (str(path), file_hash(path), path.stat().st_size),
    )
    ctx.conn.commit()


class TestRebaseline:
    def test_establishes_baseline_for_all_archive_files(self, ctx):
        _archive_file(ctx, "Artist/Album/a.m4a", b"CONTENT A")
        _archive_file(ctx, "Artist/Album/b.m4a", b"CONTENT B")
        ctx.set("bitrot_rebaseline", True)

        result = BitRotStage().run(ctx)

        assert result.success is True
        assert result.files_changed == 2
        count = ctx.conn.execute("SELECT COUNT(*) FROM archive_tier_hashes").fetchone()[0]
        assert count == 2

    def test_rebaseline_overwrites_existing_entry(self, ctx):
        path = _archive_file(ctx, "Artist/Album/a.m4a", b"ORIGINAL")
        _baseline(ctx, path)
        path.write_bytes(b"CHANGED -- THIS IS NOW THE TRUSTED STATE")
        ctx.set("bitrot_rebaseline", True)

        BitRotStage().run(ctx)

        row = ctx.conn.execute(
            "SELECT sha256 FROM archive_tier_hashes WHERE path = ?", (str(path),)
        ).fetchone()
        assert row["sha256"] == file_hash(path)

    def test_empty_archive_reports_nothing_to_do(self, ctx):
        ctx.set("bitrot_rebaseline", True)
        result = BitRotStage().run(ctx)
        assert any("nothing to do" in n for n in result.notes)

    def test_dry_run_rebaseline_writes_nothing(self, ctx):
        _archive_file(ctx, "Artist/Album/a.m4a", b"CONTENT")
        ctx.set("bitrot_rebaseline", True)

        result = BitRotStage().dry_run(ctx)

        assert result.files_processed == 1
        count = ctx.conn.execute("SELECT COUNT(*) FROM archive_tier_hashes").fetchone()[0]
        assert count == 0


class TestVerify:
    def test_unchanged_file_reports_ok(self, ctx):
        path = _archive_file(ctx, "Artist/Album/a.m4a", b"CONTENT")
        _baseline(ctx, path)

        result = BitRotStage().run(ctx)

        assert result.success is True
        assert any("ok: 1" in n for n in result.notes)

    def test_changed_bytes_detected_as_corrupt(self, ctx):
        path = _archive_file(ctx, "Artist/Album/a.m4a", b"ORIGINAL CONTENT")
        _baseline(ctx, path)
        path.write_bytes(b"CORRUPTED!!CONTENT")  # same length, different bytes

        result = BitRotStage().run(ctx)

        assert result.success is False
        assert any("corrupt (hash mismatch): 1" in n for n in result.notes)

        events = ctx.conn.execute(
            "SELECT event_type FROM events WHERE event_type='BITROT_DETECTED'"
        ).fetchall()
        assert len(events) == 1

    def test_stored_baseline_never_overwritten_on_mismatch(self, ctx):
        path = _archive_file(ctx, "Artist/Album/a.m4a", b"ORIGINAL CONTENT")
        _baseline(ctx, path)
        original_baseline = ctx.conn.execute(
            "SELECT sha256 FROM archive_tier_hashes WHERE path = ?", (str(path),)
        ).fetchone()["sha256"]
        path.write_bytes(b"CORRUPTED!!CONTENT")

        BitRotStage().run(ctx)

        row = ctx.conn.execute(
            "SELECT sha256 FROM archive_tier_hashes WHERE path = ?", (str(path),)
        ).fetchone()
        assert row["sha256"] == original_baseline

    def test_new_file_not_flagged_corrupt(self, ctx):
        """A file that exists but was never baselined isn't corrupt --
        it just needs a --rebaseline pass."""
        _archive_file(ctx, "Artist/Album/new.m4a", b"CONTENT")

        result = BitRotStage().run(ctx)

        assert result.success is True
        assert any("new (no baseline yet" in n and "1" in n for n in result.notes)

    def test_missing_baselined_file_reported_separately(self, ctx):
        path = _archive_file(ctx, "Artist/Album/a.m4a", b"CONTENT")
        _baseline(ctx, path)
        path.unlink()

        result = BitRotStage().run(ctx)

        assert any("missing from disk" in n and "1" in n for n in result.notes)

    def test_limit_caps_files_processed(self, ctx):
        for i in range(5):
            p = _archive_file(ctx, f"Artist/Album/{i}.m4a", f"CONTENT {i}".encode())
            _baseline(ctx, p)
        ctx.set("bitrot_limit", 2)

        result = BitRotStage().run(ctx)

        assert result.files_processed == 2

    def test_dry_run_makes_no_changes(self, ctx):
        path = _archive_file(ctx, "Artist/Album/a.m4a", b"CONTENT")
        _baseline(ctx, path)

        result = BitRotStage().dry_run(ctx)

        assert result.files_processed == 1
        # RunContext.new()/record_stage() log their own framework-level
        # events (RUN_START/STAGE_COMPLETE) regardless of stage or mode --
        # that's universal, not something to assert against here. What
        # actually matters: no bit-rot-specific event, no baseline write.
        bitrot_events = ctx.conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'BITROT_DETECTED'"
        ).fetchone()[0]
        assert bitrot_events == 0

    def test_alac_library_content_never_scanned(self, ctx):
        """This stage checks ALAC_Archive specifically -- ALAC-Library
        gets legitimately rewritten by LUFS baking, so "did it change" is
        never a meaningful question to ask about it here."""
        lib_file = ctx.alac_library / "Artist" / "Album" / "baked.m4a"
        lib_file.parent.mkdir(parents=True, exist_ok=True)
        lib_file.write_bytes(b"BAKED CONTENT")

        result = BitRotStage().run(ctx)

        assert result.files_processed == 0
