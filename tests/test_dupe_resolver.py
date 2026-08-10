"""
Tests for DupeResolverStage — physically relocate duplicate-group losers
into ALAC-Library/DUPES_MOVED_FOR_REVIEW/<date>/Artist/Album/Track,
mirroring ALAC-Library's own shape, per Grey's explicit instruction.

Uses real files on disk (not mocked): this stage's whole purpose is a
real filesystem move, a real manifest, and a real restore script -- a
mock would hide exactly the kind of bug a real move can produce.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db
from musaeus.stages.dupe_resolver import DupeResolverStage

_TEST_BATCH_DATE = "2026-01-15"


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
    c = RunContext.new(cfg, conn, dry_run=False)
    c.set("finalize_batch_date", _TEST_BATCH_DATE)
    return c


@pytest.fixture
def ctx_dry(cfg: MusicConfig) -> RunContext:
    cfg.ensure_dirs()
    conn = open_db(cfg.db_path)
    c = RunContext.new(cfg, conn, dry_run=True)
    c.set("finalize_batch_date", _TEST_BATCH_DATE)
    return c


def _make_archive_row(ctx: RunContext, relpath: str, artist: str, album: str, title: str,
                       bitrate: int, size_bytes: int) -> Path:
    path = ctx.inbox / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"X" * size_bytes)
    from musaeus.db import upsert_archive
    upsert_archive(ctx.conn, {
        "file_path": str(path), "status": "CATALOGUED",
        "artist": artist, "album": album, "title": title,
        "bitrate": bitrate, "size_bytes": size_bytes,
    })
    ctx.conn.commit()
    return path


def _stage_duplicate_pair(ctx: RunContext, group_id: str, path_high: Path, path_low: Path,
                           dtype: str = "EXACT") -> None:
    for fp in (str(path_high), str(path_low)):
        ctx.conn.execute(
            "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, run_id) "
            "VALUES (?, ?, ?, 1.0, ?)",
            (group_id, fp, dtype, ctx.run_id),
        )
    ctx.conn.commit()


class TestDupeResolverSameBatchGroup:
    def test_keeps_highest_bitrate_moves_the_rest(self, ctx):
        high = _make_archive_row(ctx, "high.flac", "Artist", "Album", "Title", bitrate=900_000, size_bytes=500)
        low = _make_archive_row(ctx, "low.m4a", "Artist", "Album", "Title", bitrate=128_000, size_bytes=200)
        _stage_duplicate_pair(ctx, "dup_test1", high, low)

        result = DupeResolverStage().execute(ctx)

        assert result.success is True
        assert result.files_changed == 1
        assert high.exists()  # keeper stays put
        assert not low.exists()  # loser moved out

        target = ctx.config.dupes_review_dir / _TEST_BATCH_DATE / "Artist" / "Album" / "Artist - Title.m4a"
        assert target.exists()

        keep_status = ctx.conn.execute(
            "SELECT status FROM duplicates WHERE file_path = ?", (str(high),)
        ).fetchone()["status"]
        archive_status = ctx.conn.execute(
            "SELECT status FROM duplicates WHERE file_path = ?", (str(low),)
        ).fetchone()["status"]
        assert keep_status == "keep"
        assert archive_status == "archive"

    def test_manifest_and_restore_script_written(self, ctx):
        high = _make_archive_row(ctx, "high.flac", "Artist", "Album", "Title", bitrate=900_000, size_bytes=500)
        low = _make_archive_row(ctx, "low.m4a", "Artist", "Album", "Title", bitrate=128_000, size_bytes=200)
        _stage_duplicate_pair(ctx, "dup_test2", high, low)

        DupeResolverStage().execute(ctx)

        review_dir = ctx.config.dupes_review_dir / _TEST_BATCH_DATE
        manifests = list(review_dir.glob("moved_manifest_*.csv"))
        scripts = list(review_dir.glob("restore_*.sh"))
        assert len(manifests) == 1
        assert len(scripts) == 1
        assert scripts[0].stat().st_mode & 0o100  # executable bit set

        content = manifests[0].read_text()
        assert "source,destination,group_id,duplicate_type" in content
        assert str(low) in content

    def test_restore_script_actually_reverses_the_move(self, ctx):
        high = _make_archive_row(ctx, "high.flac", "Artist", "Album", "Title", bitrate=900_000, size_bytes=500)
        low = _make_archive_row(ctx, "low.m4a", "Artist", "Album", "Title", bitrate=128_000, size_bytes=200)
        _stage_duplicate_pair(ctx, "dup_test3", high, low)

        DupeResolverStage().execute(ctx)
        assert not low.exists()

        review_dir = ctx.config.dupes_review_dir / _TEST_BATCH_DATE
        restore_script = next(review_dir.glob("restore_*.sh"))

        import subprocess
        subprocess.run(["bash", str(restore_script)], check=True, capture_output=True)

        assert low.exists()  # back where it started


class TestDupeResolverCodecPriority:
    def test_lossless_kept_over_lossy_despite_lower_bitrate_number(self, ctx):
        """Regression test: bitrate alone is not a fair cross-codec
        comparison -- a highly-compressible lossless FLAC can report a
        LOWER bitrate number than a lossy file of the same audio,
        despite being the objectively better copy. Real scenario found
        live during this session's testing."""
        from musaeus.db import upsert_archive

        lossy = ctx.inbox / "lossy_high_number.m4a"
        lossy.parent.mkdir(parents=True, exist_ok=True)
        lossy.write_bytes(b"X" * 1000)
        upsert_archive(ctx.conn, {
            "file_path": str(lossy), "status": "CATALOGUED",
            "artist": "Artist", "album": "Album", "title": "Title",
            "codec": "aac", "bitrate": 131_382, "size_bytes": 1000,
        })

        lossless = ctx.inbox / "lossless_low_number.flac"
        lossless.write_bytes(b"X" * 900)
        upsert_archive(ctx.conn, {
            "file_path": str(lossless), "status": "CATALOGUED",
            "artist": "Artist", "album": "Album", "title": "Title",
            "codec": "flac", "bitrate": 129_200, "size_bytes": 900,
        })
        ctx.conn.commit()

        _stage_duplicate_pair(ctx, "dup_codec_test", lossless, lossy)

        result = DupeResolverStage().execute(ctx)

        assert result.success is True
        assert lossless.exists()  # lossless kept, despite the lower bitrate number
        assert not lossy.exists()  # lossy moved to review, despite the higher bitrate number

        keep_status = ctx.conn.execute(
            "SELECT status FROM duplicates WHERE file_path = ?", (str(lossless),)
        ).fetchone()["status"]
        assert keep_status == "keep"


class TestDupeResolverCrossBatchGroup:
    def test_lone_cross_batch_member_moves_no_keeper_needed(self, ctx):
        """A CROSS_BATCH group has exactly one member in THIS batch's
        duplicates table -- the incoming file. There's no in-batch
        keeper to compare against; the incoming file simply moves."""
        incoming = _make_archive_row(ctx, "incoming.m4a", "New Artist", "New Album", "New Title",
                                      bitrate=256_000, size_bytes=300)
        ctx.conn.execute(
            "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, run_id) "
            "VALUES (?, ?, 'CROSS_BATCH', 1.0, ?)",
            ("crossdupe_abc", str(incoming), ctx.run_id),
        )
        ctx.conn.commit()

        result = DupeResolverStage().execute(ctx)

        assert result.success is True
        assert result.files_changed == 1
        assert not incoming.exists()
        target = ctx.config.dupes_review_dir / _TEST_BATCH_DATE / "New Artist" / "New Album" / "New Artist - New Title.m4a"
        assert target.exists()


class TestDupeResolverIdempotency:
    def test_already_resolved_group_not_touched_again(self, ctx):
        high = _make_archive_row(ctx, "high.flac", "Artist", "Album", "Title", bitrate=900_000, size_bytes=500)
        low = _make_archive_row(ctx, "low.m4a", "Artist", "Album", "Title", bitrate=128_000, size_bytes=200)
        _stage_duplicate_pair(ctx, "dup_test4", high, low)

        first = DupeResolverStage().execute(ctx)
        assert first.files_changed == 1

        second = DupeResolverStage().execute(ctx)
        assert second.files_processed == 0  # nothing pending anymore


class TestDupeResolverErrorHandling:
    def test_missing_file_reported_not_crash(self, ctx):
        high = _make_archive_row(ctx, "high.flac", "Artist", "Album", "Title", bitrate=900_000, size_bytes=500)
        low_path = ctx.inbox / "vanished.m4a"  # never actually created
        from musaeus.db import upsert_archive
        upsert_archive(ctx.conn, {
            "file_path": str(low_path), "status": "CATALOGUED",
            "artist": "Artist", "album": "Album", "title": "Title",
            "bitrate": 64_000, "size_bytes": 100,
        })
        ctx.conn.commit()
        _stage_duplicate_pair(ctx, "dup_test5", high, low_path)

        result = DupeResolverStage().execute(ctx)

        assert result.files_errored == 1
        assert any("missing on disk" in e for e in result.errors)


class TestDupeResolverDryRun:
    def test_dry_run_makes_no_changes(self, ctx_dry):
        high = _make_archive_row(ctx_dry, "high.flac", "Artist", "Album", "Title", bitrate=900_000, size_bytes=500)
        low = _make_archive_row(ctx_dry, "low.m4a", "Artist", "Album", "Title", bitrate=128_000, size_bytes=200)
        _stage_duplicate_pair(ctx_dry, "dup_test6", high, low)

        result = DupeResolverStage().execute(ctx_dry)

        assert result.dry_run is True
        assert result.files_changed == 1
        assert low.exists()  # nothing actually moved
        assert high.exists()

        review_dir = ctx_dry.config.dupes_review_dir / _TEST_BATCH_DATE
        assert not review_dir.exists() or not list(review_dir.glob("*.csv"))

        status = ctx_dry.conn.execute(
            "SELECT status FROM duplicates WHERE file_path = ?", (str(low),)
        ).fetchone()["status"]
        assert status == "pending"  # unchanged
