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
from musaeus.stages.dupe_resolver import DupeResolverStage, _keeper_sort_key

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


def _make_archive_row(
    ctx: RunContext,
    relpath: str,
    artist: str,
    album: str,
    title: str,
    bitrate: int,
    size_bytes: int,
) -> Path:
    path = ctx.inbox / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"X" * size_bytes)
    from musaeus.db import upsert_archive

    upsert_archive(
        ctx.conn,
        {
            "file_path": str(path),
            "status": "CATALOGUED",
            "artist": artist,
            "album": album,
            "title": title,
            "bitrate": bitrate,
            "size_bytes": size_bytes,
        },
    )
    ctx.conn.commit()
    return path


def _stage_duplicate_pair(
    ctx: RunContext, group_id: str, path_high: Path, path_low: Path, dtype: str = "EXACT"
) -> None:
    for fp in (str(path_high), str(path_low)):
        ctx.conn.execute(
            "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, run_id) "
            "VALUES (?, ?, ?, 1.0, ?)",
            (group_id, fp, dtype, ctx.run_id),
        )
    ctx.conn.commit()


class TestDupeResolverSameBatchGroup:
    def test_keeps_highest_bitrate_moves_the_rest(self, ctx):
        high = _make_archive_row(
            ctx, "high.flac", "Artist", "Album", "Title", bitrate=900_000, size_bytes=500
        )
        low = _make_archive_row(
            ctx, "low.m4a", "Artist", "Album", "Title", bitrate=128_000, size_bytes=200
        )
        _stage_duplicate_pair(ctx, "dup_test1", high, low)

        result = DupeResolverStage().execute(ctx)

        assert result.success is True
        assert result.files_changed == 1
        assert high.exists()  # keeper stays put
        assert not low.exists()  # loser moved out

        target = (
            ctx.config.dupes_review_dir
            / _TEST_BATCH_DATE
            / "Artist"
            / "Album"
            / "Artist - Title.m4a"
        )
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
        high = _make_archive_row(
            ctx, "high.flac", "Artist", "Album", "Title", bitrate=900_000, size_bytes=500
        )
        low = _make_archive_row(
            ctx, "low.m4a", "Artist", "Album", "Title", bitrate=128_000, size_bytes=200
        )
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
        high = _make_archive_row(
            ctx, "high.flac", "Artist", "Album", "Title", bitrate=900_000, size_bytes=500
        )
        low = _make_archive_row(
            ctx, "low.m4a", "Artist", "Album", "Title", bitrate=128_000, size_bytes=200
        )
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
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(lossy),
                "status": "CATALOGUED",
                "artist": "Artist",
                "album": "Album",
                "title": "Title",
                "codec": "aac",
                "bitrate": 131_382,
                "size_bytes": 1000,
            },
        )

        lossless = ctx.inbox / "lossless_low_number.flac"
        lossless.write_bytes(b"X" * 900)
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(lossless),
                "status": "CATALOGUED",
                "artist": "Artist",
                "album": "Album",
                "title": "Title",
                "codec": "flac",
                "bitrate": 129_200,
                "size_bytes": 900,
            },
        )
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


class TestDupeResolverArchiveRowFollowsFile:
    def test_archive_file_path_updated_to_new_location(self, ctx):
        """Regression test: the loser's archive row must follow the file
        to its new location, not just the duplicates table. Found via a
        full-chain dry run -- Canonicalize picked up a DupeResolver-
        relocated row still pointing at the old (now-empty) path and
        errored on 'file missing on disk'."""
        high = _make_archive_row(
            ctx, "high.flac", "Artist", "Album", "Title", bitrate=900_000, size_bytes=500
        )
        low = _make_archive_row(
            ctx, "low.m4a", "Artist", "Album", "Title", bitrate=128_000, size_bytes=200
        )
        _stage_duplicate_pair(ctx, "dup_followpath", high, low)

        DupeResolverStage().execute(ctx)

        target = (
            ctx.config.dupes_review_dir
            / _TEST_BATCH_DATE
            / "Artist"
            / "Album"
            / "Artist - Title.m4a"
        )
        row = ctx.conn.execute(
            "SELECT file_path, status FROM archive WHERE id = (SELECT id FROM archive WHERE file_path = ?)",
            (str(target),),
        ).fetchone()
        assert row is not None, "no archive row found at the new location"
        assert row["file_path"] == str(target)
        assert row["status"] == "DUPE_REVIEW"

        # And the OLD path must have no row left claiming CATALOGUED status
        # (which is what would make Canonicalize/Forge/etc pick it up again).
        stale = ctx.conn.execute(
            "SELECT status FROM archive WHERE file_path = ?", (str(low),)
        ).fetchone()
        assert stale is None  # no row at the old path anymore


class TestDupeResolverCrossBatchGroup:
    def test_lone_cross_batch_member_moves_no_keeper_needed(self, ctx):
        """A CROSS_BATCH group has exactly one member in THIS batch's
        duplicates table -- the incoming file. There's no in-batch
        keeper to compare against; the incoming file simply moves."""
        incoming = _make_archive_row(
            ctx,
            "incoming.m4a",
            "New Artist",
            "New Album",
            "New Title",
            bitrate=256_000,
            size_bytes=300,
        )
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
        target = (
            ctx.config.dupes_review_dir
            / _TEST_BATCH_DATE
            / "New Artist"
            / "New Album"
            / "New Artist - New Title.m4a"
        )
        assert target.exists()


class TestDupeResolverIdempotency:
    def test_already_resolved_group_not_touched_again(self, ctx):
        high = _make_archive_row(
            ctx, "high.flac", "Artist", "Album", "Title", bitrate=900_000, size_bytes=500
        )
        low = _make_archive_row(
            ctx, "low.m4a", "Artist", "Album", "Title", bitrate=128_000, size_bytes=200
        )
        _stage_duplicate_pair(ctx, "dup_test4", high, low)

        first = DupeResolverStage().execute(ctx)
        assert first.files_changed == 1

        second = DupeResolverStage().execute(ctx)
        assert second.files_processed == 0  # nothing pending anymore


class TestDupeResolverErrorHandling:
    def test_missing_file_reported_not_crash(self, ctx):
        high = _make_archive_row(
            ctx, "high.flac", "Artist", "Album", "Title", bitrate=900_000, size_bytes=500
        )
        low_path = ctx.inbox / "vanished.m4a"  # never actually created
        from musaeus.db import upsert_archive

        upsert_archive(
            ctx.conn,
            {
                "file_path": str(low_path),
                "status": "CATALOGUED",
                "artist": "Artist",
                "album": "Album",
                "title": "Title",
                "bitrate": 64_000,
                "size_bytes": 100,
            },
        )
        ctx.conn.commit()
        _stage_duplicate_pair(ctx, "dup_test5", high, low_path)

        result = DupeResolverStage().execute(ctx)

        assert result.files_errored == 1
        assert any("missing on disk" in e for e in result.errors)

    def test_missing_file_already_resolved_by_prior_run_is_skipped_not_errored(self, ctx):
        """Regression test for the 2026-08-18 finding: 422 duplicates-table
        rows stuck 'pending' forever because a file staged into more than
        one group across *separate runs* (not covered by the within-run
        already_moved dict) gets its first group's move recorded in the
        events log, but a second, later-created group for the same
        original path is left permanently 'pending' -- re-erroring on the
        same already-moved file every future run. Simulates that exact
        state: a duplicates row pointing at a path that's gone, plus a
        DUPE_MOVED_FOR_REVIEW event proving a prior run already handled it."""
        high = _make_archive_row(
            ctx, "high.flac", "Artist", "Album", "Title", bitrate=900_000, size_bytes=500
        )
        already_moved_source = ctx.inbox / "already_moved.m4a"  # never created --
        # simulates a file a PRIOR run already physically relocated.
        ctx.conn.execute(
            "INSERT INTO events (run_id, event_type, file_path, old_value, new_value, stage) "
            "VALUES ('prior_run', 'DUPE_MOVED_FOR_REVIEW', ?, ?, ?, 'dupe_resolver')",
            (
                str(already_moved_source) + ".moved",
                str(already_moved_source),
                str(already_moved_source) + ".moved",
            ),
        )
        ctx.conn.commit()
        from musaeus.db import upsert_archive

        upsert_archive(
            ctx.conn,
            {
                "file_path": str(already_moved_source),
                "status": "CATALOGUED",
                "artist": "Artist",
                "album": "Album",
                "title": "Title",
                "bitrate": 64_000,
                "size_bytes": 100,
            },
        )
        ctx.conn.commit()
        _stage_duplicate_pair(ctx, "dup_stale_pending", high, already_moved_source)

        result = DupeResolverStage().execute(ctx)

        assert result.files_errored == 0
        assert not any("missing on disk" in e for e in result.errors)
        assert any("already resolved by a prior run" in n for n in result.notes)

        status = ctx.conn.execute(
            "SELECT status FROM duplicates WHERE group_id = ? AND file_path = ?",
            ("dup_stale_pending", str(already_moved_source)),
        ).fetchone()[0]
        assert status == "archive"


class TestDupeResolverManifestEnrichment:
    def test_manifest_includes_kept_and_moved_codec_bitrate(self, ctx):
        """Grey's explicit ask (2026-08-12): the manifest CSV must show
        the actual codec/bitrate signal behind each decision per row,
        not just the decision itself -- otherwise reviewing a group
        means manually joining archive.codec/bitrate by hand."""
        from musaeus.db import upsert_archive

        keep = ctx.inbox / "keep.flac"
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_bytes(b"X" * 900)
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(keep),
                "status": "CATALOGUED",
                "artist": "Artist",
                "album": "Album",
                "title": "Title",
                "codec": "flac",
                "bitrate": 900_000,
                "size_bytes": 900,
            },
        )
        lose = ctx.inbox / "lose.mp3"
        lose.write_bytes(b"X" * 300)
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(lose),
                "status": "CATALOGUED",
                "artist": "Artist",
                "album": "Album",
                "title": "Title",
                "codec": "mp3",
                "bitrate": 128_000,
                "size_bytes": 300,
            },
        )
        ctx.conn.commit()
        _stage_duplicate_pair(ctx, "dup_enrich_test", keep, lose)

        DupeResolverStage().execute(ctx)

        manifest = next(
            (ctx.config.dupes_review_dir / _TEST_BATCH_DATE).glob("moved_manifest_*.csv")
        )
        content = manifest.read_text()
        header = content.splitlines()[0]
        assert header == (
            "source,destination,group_id,duplicate_type,"
            "moved_codec,moved_bitrate,kept_path,kept_codec,kept_bitrate"
        )
        data_line = content.splitlines()[1]
        assert str(lose) in data_line
        assert "mp3" in data_line
        assert "128000" in data_line
        assert str(keep) in data_line
        assert "flac" in data_line
        assert "900000" in data_line


class TestDupeResolverLiveExactHashCluster:
    """Regression test for the 2026-08-12 incident: `musaeus dedupe
    --auto`/manual review only ever flips duplicates.status -- it never
    moves a file, and duplicates.file_path goes stale once a file is
    later finalized. Confirmed in the real vault: 6,434 EXACT-type
    duplicates rows had a stale 'archive' decision that was never
    enforced, producing thousands of literal duplicate files sitting
    side by side in ALAC-Library. These tests prove DupeResolver now
    catches live audio_hash collisions among CATALOGUED rows directly,
    with NO corresponding duplicates-table entry at all -- the exact
    shape of the real bug, not a fixture that assumes the old (broken)
    detection path would have caught it."""

    def test_catches_hash_collision_with_no_duplicates_table_row(self, ctx):
        from musaeus.db import upsert_archive

        keep = ctx.inbox / "keeper.flac"
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_bytes(b"X" * 900)
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(keep),
                "status": "CATALOGUED",
                "artist": "Artist",
                "album": "Album",
                "title": "Title",
                "codec": "alac",
                "bitrate": 900_000,
                "size_bytes": 900,
                "audio_hash": "sharedhash123",
            },
        )
        stale_dupe = ctx.inbox / "already_finalized_duplicate.m4a"
        stale_dupe.write_bytes(b"X" * 300)
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(stale_dupe),
                "status": "CATALOGUED",
                "artist": "Artist",
                "album": "Album",
                "title": "Title",
                "codec": "alac",
                "bitrate": 300_000,
                "size_bytes": 300,
                "audio_hash": "sharedhash123",
            },
        )
        ctx.conn.commit()
        # Deliberately NOT inserting any row into `duplicates` -- this is
        # the exact shape of the real bug: nothing in the duplicates
        # table points at these two rows at all anymore.

        result = DupeResolverStage().execute(ctx)

        assert result.success is True
        assert result.files_changed == 1
        assert keep.exists()  # higher-bitrate copy kept in place
        assert not stale_dupe.exists()  # lower-bitrate copy moved out

        row = ctx.conn.execute(
            "SELECT status, file_path FROM archive WHERE file_path = ?", (str(keep),)
        ).fetchone()
        assert row["status"] == "CATALOGUED"  # keeper untouched

        moved_row = ctx.conn.execute(
            "SELECT status FROM archive WHERE file_path LIKE ?",
            (f"%{stale_dupe.stem}%",),
        ).fetchone()
        assert moved_row is None or moved_row["status"] != "CATALOGUED"

    def test_ignores_catalogued_rows_with_distinct_hashes(self, ctx):
        """Sanity guard: two different, non-duplicate files must never
        be treated as a cluster just because they're both CATALOGUED."""
        from musaeus.db import upsert_archive

        a = ctx.inbox / "song_a.flac"
        a.parent.mkdir(parents=True, exist_ok=True)
        a.write_bytes(b"X" * 900)
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(a),
                "status": "CATALOGUED",
                "artist": "Artist",
                "album": "Album",
                "title": "Song A",
                "codec": "flac",
                "bitrate": 900_000,
                "size_bytes": 900,
                "audio_hash": "hash_a",
            },
        )
        b = ctx.inbox / "song_b.flac"
        b.write_bytes(b"Y" * 900)
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(b),
                "status": "CATALOGUED",
                "artist": "Artist",
                "album": "Album",
                "title": "Song B",
                "codec": "flac",
                "bitrate": 900_000,
                "size_bytes": 900,
                "audio_hash": "hash_b",
            },
        )
        ctx.conn.commit()

        result = DupeResolverStage().execute(ctx)

        assert result.files_processed == 0
        assert a.exists()
        assert b.exists()

    def test_ignores_rows_already_quarantined(self, ctx):
        """A row already at status='DUPE_REVIEW' (already resolved,
        whether by this stage or a prior pass) must not be re-clustered
        just because it happens to share an audio_hash with something
        still CATALOGUED."""
        from musaeus.db import upsert_archive

        keep = ctx.inbox / "keeper2.flac"
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_bytes(b"X" * 900)
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(keep),
                "status": "CATALOGUED",
                "artist": "Artist",
                "album": "Album",
                "title": "Title",
                "codec": "alac",
                "bitrate": 900_000,
                "size_bytes": 900,
                "audio_hash": "already_handled_hash",
            },
        )
        already_moved = ctx.config.dupes_review_dir / "2026-01-14" / "old.m4a"
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(already_moved),
                "status": "DUPE_REVIEW",
                "artist": "Artist",
                "album": "Album",
                "title": "Title",
                "codec": "alac",
                "bitrate": 300_000,
                "size_bytes": 300,
                "audio_hash": "already_handled_hash",
            },
        )
        ctx.conn.commit()

        result = DupeResolverStage().execute(ctx)

        assert result.files_processed == 0
        assert keep.exists()


class TestDupeResolverDryRun:
    def test_dry_run_makes_no_changes(self, ctx_dry):
        high = _make_archive_row(
            ctx_dry, "high.flac", "Artist", "Album", "Title", bitrate=900_000, size_bytes=500
        )
        low = _make_archive_row(
            ctx_dry, "low.m4a", "Artist", "Album", "Title", bitrate=128_000, size_bytes=200
        )
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


class TestDupeResolverOverlappingGroups:
    """Reproduces the 2026-08-14 real-run failure: a single physical file
    staged as a loser in more than one duplicates-table group (e.g. flagged
    independently by both the EXACT/NEAR detector and the CROSS_BATCH
    detector -- the same underlying file, two different group_ids). Before
    the fix, the second group to reach that file found its own snapshot of
    duplicates.file_path pointing at a path the first group had already
    moved, and misreported a legitimate prior move as "file missing on
    disk" (51,310 errors in the real run, ~19,000 of them exactly this)."""

    def test_second_group_referencing_same_file_is_skipped_not_errored(self, ctx):
        high = _make_archive_row(
            ctx, "high.flac", "Artist", "Album", "Title", bitrate=900_000, size_bytes=500
        )
        low = _make_archive_row(
            ctx, "low.m4a", "Artist", "Album", "Title", bitrate=128_000, size_bytes=200
        )
        # low is staged as a loser in TWO different groups -- the real-world
        # shape of a file independently flagged by two detectors.
        _stage_duplicate_pair(ctx, "dup_group_a", high, low)
        ctx.conn.execute(
            "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, run_id) "
            "VALUES (?, ?, ?, 1.0, ?)",
            ("dup_group_b", str(low), "CROSS_BATCH", ctx.run_id),
        )
        ctx.conn.commit()

        result = DupeResolverStage().execute(ctx)

        assert result.files_changed == 1  # moved exactly once
        assert result.files_errored == 0  # NOT reported as "file missing on disk"
        assert not low.exists()  # actually moved
        assert high.exists()  # the keeper stays

    def test_a_file_kept_by_one_group_is_not_moved_as_anothers_loser(self, ctx):
        """The gap `already_moved` never closed.

        Overlapping groups reached contradictory verdicts on the same file
        and nothing reconciled them: measured on the live vault 2026-08-31,
        2,918 NEAR files sit in more than one group, and "Al Green - Let's
        Stay Together" is `keep` in near_f122326e and `archive` in
        near_2231ee82 simultaneously. Which verdict won was decided by
        whichever group happened to be processed last.

        `already_moved` only suppressed the misleading "file missing on
        disk" error when a later group met an earlier group's move. It did
        not stop that group deciding to move a file an earlier group had
        chosen to keep -- it made the wrong outcome quiet rather than
        preventing it.

        Groups are now merged into connected components before resolution,
        so one keeper serves the whole component and the contradiction is
        unrepresentable rather than merely unlikely.
        """
        best = _make_archive_row(
            ctx, "best.flac", "Artist", "Album", "Title", bitrate=900_000, size_bytes=500
        )
        middle = _make_archive_row(
            ctx, "middle.m4a", "Artist", "Album", "Title", bitrate=500_000, size_bytes=300
        )
        worst = _make_archive_row(
            ctx, "worst.m4a", "Artist", "Album", "Title", bitrate=128_000, size_bytes=200
        )
        # middle LOSES to best in one group and WINS over worst in another.
        # Resolved per-group, group two keeps a file group one just moved.
        _stage_duplicate_pair(ctx, "grp_one", best, middle)
        _stage_duplicate_pair(ctx, "grp_two", middle, worst)

        result = DupeResolverStage().execute(ctx)

        assert best.exists(), "the component's single best copy must survive"
        assert not middle.exists(), "middle loses to best and must move"
        assert not worst.exists(), "worst loses to best and must move"
        assert result.files_errored == 0
        assert result.files_changed == 2, "exactly the two non-keepers move"


class TestOriginalTrumpsRemaster:
    """Grey's rule, 2026-08-21: a remaster and its original are the SAME
    song for grouping, but when one must be kept the original wins."""

    def _m(self, **kw):
        base = {"codec": "alac", "bitrate": 1000, "size_bytes": 1000, "title": "", "album": ""}
        base.update(kw)
        return base

    def test_original_beats_remaster(self):
        original = self._m(title="A Monday Date")
        remaster = self._m(title="A Monday Date (Remastered)")
        assert sorted([remaster, original], key=_keeper_sort_key)[0] is original

    def test_reissue_marker_read_from_album_too(self):
        # The marker lands in whichever field the source used.
        original = self._m(title="Song", album="Greatest Hits")
        remaster = self._m(title="Song", album="Chicago High Life (2013 Remaster)")
        assert sorted([remaster, original], key=_keeper_sort_key)[0] is original

    def test_lossless_remaster_still_beats_lossy_original(self):
        """Codec outranks the reissue rule deliberately.

        The preference is about which *release* to keep, not a licence to
        keep a worse file -- a lossless remaster is a better artifact than
        a lossy original.
        """
        lossy_original = self._m(codec="aac", title="Song")
        lossless_remaster = self._m(codec="alac", title="Song (Remastered)")
        assert (
            sorted([lossy_original, lossless_remaster], key=_keeper_sort_key)[0]
            is lossless_remaster
        )

    def test_reissue_outranks_bitrate(self):
        """A remaster is often louder and larger without being wanted."""
        original = self._m(title="Song", bitrate=900)
        remaster = self._m(title="Song (Remaster)", bitrate=1500)
        assert sorted([remaster, original], key=_keeper_sort_key)[0] is original

    def test_plain_titles_unaffected(self):
        a = self._m(title="Song", bitrate=1200)
        b = self._m(title="Song", bitrate=800)
        assert sorted([b, a], key=_keeper_sort_key)[0] is a


class TestStudioOverLive:
    """Grey's third keeper rule, confirmed 2026-08-22.

    Full order: (1) lossless over lossy, (2) original over remaster when
    both are the same codec, (3) studio over live when both are the same
    codec AND type. Each rank only decides groups the ranks above it tie
    on -- which is why a live original still beats a studio remaster.
    """

    def _m(self, **kw):
        base = {"codec": "alac", "bitrate": 1000, "size_bytes": 1000, "title": "", "album": ""}
        base.update(kw)
        return base

    def test_studio_beats_live_all_else_equal(self):
        studio = self._m(title="Stormy Monday")
        live = self._m(title="Stormy Monday (live at Fillmore East)")
        assert sorted([live, studio], key=_keeper_sort_key)[0] is studio

    def test_live_marker_read_from_album_too(self):
        studio = self._m(title="Layla", album="Layla and Other Assorted Love Songs")
        live = self._m(title="Layla", album="Unplugged")
        assert sorted([live, studio], key=_keeper_sort_key)[0] is studio

    def test_lossless_live_still_beats_lossy_studio(self):
        """Codec is the binding constraint; the room comes far behind it."""
        lossy_studio = self._m(codec="aac", title="Song")
        lossless_live = self._m(codec="alac", title="Song (Live)")
        assert sorted([lossy_studio, lossless_live], key=_keeper_sort_key)[0] is lossless_live

    def test_live_original_beats_studio_remaster(self):
        """Rank 2 (reissue) outranks rank 3 (live), so this is correct.

        Studio-over-live only decides groups that tie on codec AND reissue
        status -- "same codec and type" in Grey's wording.
        """
        live_original = self._m(title="Song (Live)")
        studio_remaster = self._m(title="Song (Remastered)")
        assert sorted([live_original, studio_remaster], key=_keeper_sort_key)[0] is live_original

    def test_studio_wins_before_bitrate_is_considered(self):
        """A louder, larger live take must not outrank the studio cut."""
        studio = self._m(title="Song", bitrate=900)
        live = self._m(title="Song (Live)", bitrate=1500)
        assert sorted([live, studio], key=_keeper_sort_key)[0] is studio

    def test_plain_titles_unaffected(self):
        hi = self._m(title="Song", bitrate=1200)
        lo = self._m(title="Song", bitrate=800)
        assert sorted([lo, hi], key=_keeper_sort_key)[0] is hi


class TestOrderingAndRelocation:
    """Two defects found by a five-file test batch on 2026-08-25.

    The stage moved the loser and then wrote its row, so a failed write
    left the file relocated and the row pointing nowhere (scope §4.25).

    And it treated any missing source as lost, recognising a prior move
    only when *this stage* had recorded it. A file relocated by another
    stage -- ClassicalComposer refiling under a composer, a manual
    restore -- reads identically from here, and five such rows failed the
    whole stage with rc=1 while every one of the files sat safely on disk.
    """

    def test_a_failed_move_leaves_the_row_untouched(self, ctx, monkeypatch):
        keeper = _make_archive_row(ctx, "keep.m4a", "A", "Al", "Song", 900, 900)
        loser = _make_archive_row(ctx, "lose.m4a", "A", "Al", "Song", 100, 100)
        _stage_duplicate_pair(ctx, "grp1", keeper, loser)

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr("musaeus.stages.dupe_resolver.shutil.move", boom)

        DupeResolverStage().run(ctx)

        status = ctx.conn.execute(
            "SELECT status FROM archive WHERE file_path = ?", (str(loser),)
        ).fetchone()["status"]
        assert status == "CATALOGUED", "the row must not claim a move that failed"
        assert loser.exists()

    def test_a_file_relocated_by_another_stage_is_skipped_not_failed(self, ctx):
        keeper = _make_archive_row(ctx, "keep2.m4a", "B", "Al", "Song", 900, 900)
        loser = _make_archive_row(ctx, "lose2.m4a", "B", "Al", "Song", 100, 100)
        _stage_duplicate_pair(ctx, "grp2", keeper, loser)

        # another stage moved it, recorded under its own event type
        moved_to = ctx.inbox / "elsewhere.m4a"
        loser.rename(moved_to)
        ctx.conn.execute(
            "INSERT INTO events (run_id,event_type,file_path,old_value,new_value,stage,note) "
            "VALUES (?,?,?,?,?,?,?)",
            ("x", "ARTIST_SET_TO_COMPOSER", str(moved_to), str(loser), str(moved_to), "other", ""),
        )
        ctx.conn.commit()

        result = DupeResolverStage().run(ctx)

        assert result.files_errored == 0, "a relocated file is not a lost file"
        assert any("relocated by another stage" in n for n in result.notes)

    def test_a_stale_row_with_no_archive_record_is_skipped_not_failed(self, ctx):
        """A duplicates-table entry naming a path the library no longer
        tracks is stale history — an old INBOX path from before ingest
        moved the file, or one already handled. Not a lost file.

        Safe to key on the missing archive row: a genuinely lost file KEEPS
        its row pointing at the gone path, and doctor's "rows with a missing
        file" check is what catches that.
        """
        keeper = _make_archive_row(ctx, "keep3.m4a", "C", "Al", "Song", 900, 900)
        loser = _make_archive_row(ctx, "lose3.m4a", "C", "Al", "Song", 100, 100)
        _stage_duplicate_pair(ctx, "grp3", keeper, loser)

        # the library stopped tracking that path entirely, and the file is gone
        ctx.conn.execute("DELETE FROM archive WHERE file_path = ?", (str(loser),))
        ctx.conn.commit()
        loser.unlink()

        result = DupeResolverStage().run(ctx)

        assert result.files_errored == 0
        assert any("no archive row for this path" in n for n in result.notes)

    def test_a_genuinely_lost_file_still_errors(self, ctx):
        """The row survives, so this must NOT be swallowed as stale."""
        keeper = _make_archive_row(ctx, "keep4.m4a", "D", "Al", "Song", 900, 900)
        loser = _make_archive_row(ctx, "lose4.m4a", "D", "Al", "Song", 100, 100)
        _stage_duplicate_pair(ctx, "grp4", keeper, loser)

        loser.unlink()  # file gone, archive row intact

        result = DupeResolverStage().run(ctx)

        assert result.files_errored == 1
        assert any("file missing on disk" in e for e in result.errors)
