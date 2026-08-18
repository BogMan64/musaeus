"""
Tests for AuditStage — physical-presence verification gate before a
batch's musaeus.db can be snapshotted and wiped.

Uses real files on disk and a real persistent hash index (not mocked):
this stage's entire purpose is comparing real disk state against real
DB state and a real separate index file, so a mock would hide exactly
the kind of drift it exists to catch.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, open_hash_index, record_finalized_hash, upsert_archive
from musaeus.stages.audit import AuditStage

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")


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


def _gen_audio(path: Path, freq: int = 440) -> None:
    import subprocess

    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration=1",
            "-c:a",
            "alac",
            str(path),
        ],
        capture_output=True,
        check=True,
    )


def _make_finalized_row(ctx: RunContext, relpath: str, audio_hash_val: str = "hash123") -> Path:
    """Create a real file directly at its ALAC-Library location, with a
    matching finalized archive row AND a matching persistent hash-index
    entry -- the fully-consistent state Audit should approve."""
    path = ctx.alac_library / relpath
    _gen_audio(path)
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(path),
            "status": "CATALOGUED",
            "audio_hash": audio_hash_val,
        },
    )
    ctx.conn.execute(
        "UPDATE archive SET finalized_at = datetime('now') WHERE file_path = ?",
        (str(path),),
    )
    ctx.conn.commit()

    hash_conn = open_hash_index(ctx.config.hash_index_path)
    record_finalized_hash(hash_conn, audio_hash_val, str(path))
    hash_conn.commit()
    hash_conn.close()

    return path


class TestAuditCleanState:
    def test_fully_consistent_state_passes(self, ctx):
        _make_finalized_row(ctx, "Artist/Album/Track.m4a")

        result = AuditStage().execute(ctx)

        assert result.success is True
        assert result.files_errored == 0
        assert any("AUDIT PASSED" in n for n in result.notes)

    def test_no_finalized_rows_is_a_clean_pass(self, ctx):
        result = AuditStage().execute(ctx)
        assert result.success is True


class TestAuditMissingFile:
    def test_finalized_row_but_file_deleted(self, ctx):
        path = _make_finalized_row(ctx, "Artist/Album/Track.m4a")
        path.unlink()

        result = AuditStage().execute(ctx)

        assert result.success is False
        assert any("missing on disk" in e for e in result.errors)


class TestAuditOrphanFile:
    def test_file_in_library_with_no_finalized_row(self, ctx):
        orphan = ctx.alac_library / "Sneaky" / "Sneaky.m4a"
        _gen_audio(orphan)
        # Deliberately no archive row, no finalized_at, no hash-index entry.

        result = AuditStage().execute(ctx)

        assert result.success is False
        assert any("no matching finalized row" in e for e in result.errors)

    def test_dupes_review_dir_excluded_from_orphan_scan(self, ctx):
        """Files under DUPES_MOVED_FOR_REVIEW are a separate mechanism,
        not expected to have a normal finalized archive row -- must not
        be flagged as orphans."""
        held = ctx.config.dupes_review_dir / "EXACT" / "group1" / "loser.m4a"
        _gen_audio(held)

        result = AuditStage().execute(ctx)

        assert result.success is True


class TestAuditHashIndexMismatch:
    def test_finalized_row_missing_from_hash_index(self, ctx):
        path = ctx.alac_library / "Artist" / "Album" / "Track.m4a"
        _gen_audio(path)
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(path),
                "status": "CATALOGUED",
                "audio_hash": "hash999",
            },
        )
        ctx.conn.execute(
            "UPDATE archive SET finalized_at = datetime('now') WHERE file_path = ?",
            (str(path),),
        )
        ctx.conn.commit()
        # Deliberately never write to the hash index at all.

        result = AuditStage().execute(ctx)

        assert result.success is False
        assert any(
            "hash index file was ever created" in e or "hash missing from persistent" in e
            for e in result.errors
        )

    def test_hash_index_exists_but_missing_this_specific_entry(self, ctx):
        # One row correctly indexed...
        _make_finalized_row(ctx, "Artist A/Album/Track.m4a", audio_hash_val="hash_a")

        # ...a second row finalized but its hash never made it into the index.
        path_b = ctx.alac_library / "Artist B" / "Album" / "Track.m4a"
        _gen_audio(path_b, freq=880)
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(path_b),
                "status": "CATALOGUED",
                "audio_hash": "hash_b",
            },
        )
        ctx.conn.execute(
            "UPDATE archive SET finalized_at = datetime('now') WHERE file_path = ?",
            (str(path_b),),
        )
        ctx.conn.commit()

        result = AuditStage().execute(ctx)

        assert result.success is False
        assert any("hash missing from persistent" in e for e in result.errors)


class TestAuditHashIndexSurvivesPostFinalizeMove:
    def test_row_moved_after_finalize_still_matches_via_audio_hash(self, ctx):
        """Regression test for the 2026-08-18 overnight run's 10,415 false
        positives: DupeResolverStage finalizes a row, records its hash-index
        entry at the finalize-time path, then later quarantines the row into
        DUPES_MOVED_FOR_REVIEW and updates archive.file_path -- exactly like
        dupe_resolver.py's own UPDATE at line ~389. The hash-index entry is
        never touched (by design -- finalized_hashes.file_path is a
        point-in-time snapshot, not kept in sync). Audit must still find the
        hash via audio_hash alone, not require the stale path to match."""
        original_path = ctx.alac_library / "Artist" / "Album" / "Track.m4a"
        _gen_audio(original_path)
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(original_path),
                "status": "CATALOGUED",
                "audio_hash": "hash_moved",
            },
        )
        ctx.conn.execute(
            "UPDATE archive SET finalized_at = datetime('now') WHERE file_path = ?",
            (str(original_path),),
        )
        ctx.conn.commit()

        hash_conn = open_hash_index(ctx.config.hash_index_path)
        record_finalized_hash(hash_conn, "hash_moved", str(original_path))
        hash_conn.commit()
        hash_conn.close()

        # Simulate DupeResolverStage quarantining this row: file physically
        # moved, archive.file_path updated, hash index left untouched.
        quarantined_path = ctx.config.dupes_review_dir / "EXACT" / "group1" / "Track.m4a"
        quarantined_path.parent.mkdir(parents=True, exist_ok=True)
        original_path.rename(quarantined_path)
        ctx.conn.execute(
            "UPDATE archive SET status = 'DUPE_REVIEW', file_path = ? WHERE file_path = ?",
            (str(quarantined_path), str(original_path)),
        )
        ctx.conn.commit()

        result = AuditStage().execute(ctx)

        assert not any("hash missing from persistent" in e for e in result.errors)


class TestAuditReadOnly:
    def test_never_mutates_archive_or_disk(self, ctx):
        path = _make_finalized_row(ctx, "Artist/Album/Track.m4a")
        before_mtime = path.stat().st_mtime
        before_row = dict(ctx.conn.execute("SELECT * FROM archive").fetchone())

        AuditStage().execute(ctx)

        assert path.stat().st_mtime == before_mtime
        after_row = dict(ctx.conn.execute("SELECT * FROM archive").fetchone())
        assert after_row == before_row

    def test_dry_run_identical_to_run(self, ctx):
        """Audit is inherently read-only -- dry_run and run must report
        the same thing."""
        _make_finalized_row(ctx, "Artist/Album/Track.m4a")

        stage = AuditStage()
        dry_result = stage.dry_run(ctx)
        run_result = stage.run(ctx)

        assert dry_result.success == run_result.success
        assert dry_result.files_errored == run_result.files_errored
