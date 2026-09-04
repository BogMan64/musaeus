"""
Tests for SentinelStage — Stage 2: Hash files and detect exact duplicates.

Mocks audio_hash_safe and file_hash to avoid needing ffmpeg installed.
"""

import csv
from pathlib import Path
from unittest.mock import patch

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.sentinel import SentinelStage, _get_pending, _hash_group_for


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
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=False)


@pytest.fixture
def ctx_dry(cfg: MusicConfig) -> RunContext:
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=True)


def _insert_pending(ctx: RunContext, file_path: str) -> None:
    """Insert a PENDING row into archive for testing."""
    upsert_archive(ctx.conn, {"file_path": file_path, "status": "PENDING"})
    ctx.conn.commit()


# ── Validate ──────────────────────────────────────────────────────────────────


class TestSentinelValidate:
    def test_validate_no_pending_is_noop(self, ctx):
        """Validate passes even with no PENDING rows (just info log)."""
        stage = SentinelStage()
        stage.validate(ctx)  # should not raise

    def test_validate_with_pending(self, ctx, tmp_path):
        _insert_pending(ctx, str(tmp_path / "track.flac"))
        stage = SentinelStage()
        stage.validate(ctx)  # should not raise


# ── Dry run ───────────────────────────────────────────────────────────────────


class TestSentinelDryRun:
    def test_dry_run_reports_pending_count(self, ctx_dry, tmp_path):
        _insert_pending(ctx_dry, str(tmp_path / "a.flac"))
        _insert_pending(ctx_dry, str(tmp_path / "b.mp3"))
        stage = SentinelStage()
        result = stage.execute(ctx_dry)

        assert result.dry_run is True
        assert result.files_processed == 2
        assert result.files_changed == 2
        assert any("2 file(s)" in n for n in result.notes)

    def test_dry_run_no_db_changes(self, ctx_dry, tmp_path):
        _insert_pending(ctx_dry, str(tmp_path / "a.flac"))
        stage = SentinelStage()
        stage.execute(ctx_dry)

        # Status should still be PENDING (no hash written)
        row = ctx_dry.conn.execute(
            "SELECT status, audio_hash FROM archive WHERE file_path=?",
            (str(tmp_path / "a.flac"),),
        ).fetchone()
        assert row["status"] == "PENDING"
        assert row["audio_hash"] is None

    def test_dry_run_empty(self, ctx_dry):
        stage = SentinelStage()
        result = stage.execute(ctx_dry)
        assert result.files_processed == 0
        assert any("0 file(s)" in n for n in result.notes)

    def test_dry_run_truncates_long_list(self, ctx_dry, tmp_path):
        """If more than 10 pending files, notes mention 'more'."""
        for i in range(15):
            _insert_pending(ctx_dry, str(tmp_path / f"track{i:02d}.flac"))
        stage = SentinelStage()
        result = stage.execute(ctx_dry)
        assert any("more" in n for n in result.notes)


# ── Run (mocked hashing) ─────────────────────────────────────────────────────


class TestSentinelRun:
    @patch("musaeus.stages.sentinel.audio_hash_safe")
    @patch("musaeus.stages.sentinel.file_hash")
    def test_hashes_pending_file(self, mock_fh, mock_ah, ctx, tmp_path):
        # Create a real file so Path.exists() passes
        track = tmp_path / "track.flac"
        track.write_bytes(b"FAKE AUDIO")
        _insert_pending(ctx, str(track))

        mock_fh.return_value = "f" * 64
        mock_ah.return_value = ("a" * 64, None)

        stage = SentinelStage()
        result = stage.execute(ctx)

        assert result.success is True
        assert result.files_changed == 1
        row = ctx.conn.execute(
            "SELECT status, audio_hash, full_hash FROM archive WHERE file_path=?",
            (str(track),),
        ).fetchone()
        assert row["status"] == "HASHED"
        assert row["audio_hash"] == "a" * 64
        assert row["full_hash"] == "f" * 64

    @patch("musaeus.stages.sentinel.audio_hash_safe")
    @patch("musaeus.stages.sentinel.file_hash")
    def test_detects_exact_duplicate(self, mock_fh, mock_ah, ctx, tmp_path):
        """Two files with the same audio_hash → flagged as EXACT duplicate."""
        track_a = tmp_path / "a.flac"
        track_b = tmp_path / "b.flac"
        track_a.write_bytes(b"FAKE A")
        track_b.write_bytes(b"FAKE B")
        _insert_pending(ctx, str(track_a))
        _insert_pending(ctx, str(track_b))

        shared_hash = "deadbeef" * 8
        mock_fh.return_value = "f" * 64
        mock_ah.return_value = (shared_hash, None)

        stage = SentinelStage()
        result = stage.execute(ctx)

        assert result.files_changed == 2
        dupes = ctx.conn.execute("SELECT * FROM duplicates").fetchall()
        assert len(dupes) >= 2
        assert all(d["duplicate_type"] == "EXACT" for d in dupes)

    @patch("musaeus.stages.sentinel.audio_hash_safe")
    @patch("musaeus.stages.sentinel.file_hash")
    def test_handles_missing_file(self, mock_fh, mock_ah, ctx, tmp_path):
        """File removed between ingest and sentinel → errored."""
        _insert_pending(ctx, str(tmp_path / "gone.flac"))

        stage = SentinelStage()
        result = stage.execute(ctx)

        assert result.files_errored == 1
        assert any("Missing" in e for e in result.errors)

    @patch("musaeus.stages.sentinel.audio_hash_safe")
    @patch("musaeus.stages.sentinel.file_hash")
    def test_audio_hash_failure(self, mock_fh, mock_ah, ctx, tmp_path):
        """If audio_hash fails, full_hash is stored and file stays non-HASHED."""
        track = tmp_path / "bad.flac"
        track.write_bytes(b"BAD AUDIO")
        _insert_pending(ctx, str(track))

        mock_fh.return_value = "f" * 64
        mock_ah.return_value = (None, "ffmpeg not found")

        stage = SentinelStage()
        result = stage.execute(ctx)

        assert result.files_errored == 1
        row = ctx.conn.execute(
            "SELECT status, full_hash, audio_hash FROM archive WHERE file_path=?",
            (str(track),),
        ).fetchone()
        assert row["full_hash"] == "f" * 64
        assert row["audio_hash"] is None
        # Status should NOT have advanced to HASHED since audio hash failed
        assert row["status"] != "HASHED"

    @patch("musaeus.stages.sentinel.audio_hash_safe")
    @patch("musaeus.stages.sentinel.file_hash")
    def test_events_logged(self, mock_fh, mock_ah, ctx, tmp_path):
        track = tmp_path / "song.flac"
        track.write_bytes(b"DATA")
        _insert_pending(ctx, str(track))

        mock_fh.return_value = "f" * 64
        mock_ah.return_value = ("a" * 64, None)

        SentinelStage().execute(ctx)

        events = ctx.conn.execute(
            "SELECT event_type FROM events WHERE event_type='HASH_COMPUTED'"
        ).fetchall()
        assert len(events) == 1


# ── Helper functions ──────────────────────────────────────────────────────────


class TestSentinelHelpers:
    def test_get_pending_empty(self, ctx):
        assert _get_pending(ctx.conn) == []

    def test_get_pending_returns_pending_rows(self, ctx, tmp_path):
        _insert_pending(ctx, str(tmp_path / "x.flac"))
        upsert_archive(
            ctx.conn,
            {"file_path": str(tmp_path / "y.flac"), "status": "HASHED", "audio_hash": "abc"},
        )
        ctx.conn.commit()
        pending = _get_pending(ctx.conn)
        paths = [r["file_path"] for r in pending]
        assert str(tmp_path / "x.flac") in paths
        # y.flac is HASHED but has audio_hash set, so should NOT appear
        # (it's not PENDING and audio_hash is not NULL)

    def test_hash_group_for(self, ctx, tmp_path):
        upsert_archive(
            ctx.conn, {"file_path": "/a.flac", "audio_hash": "abc123", "status": "HASHED"}
        )
        upsert_archive(
            ctx.conn, {"file_path": "/b.flac", "audio_hash": "abc123", "status": "HASHED"}
        )
        upsert_archive(
            ctx.conn, {"file_path": "/c.flac", "audio_hash": "other", "status": "HASHED"}
        )
        ctx.conn.commit()
        group = _hash_group_for(ctx.conn, "abc123")
        assert len(group) == 2
        assert "/a.flac" in group
        assert "/b.flac" in group


# ── a missing file is ghosted, not erased ────────────────────────────────────
#
# Sentinel used to DELETE the archive row for any file it could not find,
# which contradicted two things at once: GhostStage exists precisely to mark
# missing files as status='GHOST' and log GHOST_FOUND, and the event log is
# meant to be the source of truth -- a deleted row leaves nothing to
# reconcile when the file returns. A temporarily unmounted drive erased its
# own history.
#
# And the dupe-match candidate set preloaded EVERY row carrying an
# audio_hash, so a phantom -- a row whose file is already gone -- was a valid
# match target and an incoming file could be quarantined as a duplicate of
# something that no longer exists. Same family as the cascade cross_dupe.py
# was hardened against; it learned to verify liveness, Sentinel had not.


class TestMissingFileIsGhostedNotDeleted:
    @patch("musaeus.stages.sentinel.audio_hash_safe")
    @patch("musaeus.stages.sentinel.file_hash")
    def test_the_row_survives_as_ghost(self, mock_fh, mock_ah, ctx, tmp_path):
        gone = str(tmp_path / "gone.flac")
        _insert_pending(ctx, gone)

        SentinelStage().execute(ctx)

        row = ctx.conn.execute(
            "SELECT status FROM archive WHERE file_path = ?", (gone,)
        ).fetchone()
        assert row is not None, "the row was DELETED; it must be marked instead"
        assert row["status"] == "GHOST"

    @patch("musaeus.stages.sentinel.audio_hash_safe")
    @patch("musaeus.stages.sentinel.file_hash")
    def test_a_ghost_found_event_is_logged(self, mock_fh, mock_ah, ctx, tmp_path):
        gone = str(tmp_path / "gone.flac")
        _insert_pending(ctx, gone)

        SentinelStage().execute(ctx)

        events = ctx.conn.execute(
            "SELECT event_type, new_value FROM events WHERE file_path = ?", (gone,)
        ).fetchall()
        assert any(e["event_type"] == "GHOST_FOUND" for e in events), (
            "the transition must be in the event log, which is the source of truth"
        )


class TestPhantomsAreNotDuplicateTargets:
    @patch("musaeus.stages.sentinel.audio_hash_safe")
    @patch("musaeus.stages.sentinel.file_hash")
    def test_a_ghost_row_is_not_matched_as_a_duplicate(
        self, mock_fh, mock_ah, ctx, tmp_path
    ):
        """A new file must not be quarantined against a row whose file is gone."""
        shared = "a" * 64
        ctx.conn.execute(
            "INSERT INTO archive (file_path, audio_hash, status) VALUES (?, ?, 'GHOST')",
            (str(tmp_path / "vanished.flac"), shared),
        )
        arrival = tmp_path / "new.flac"
        arrival.write_bytes(b"AUDIO")
        _insert_pending(ctx, str(arrival))
        ctx.conn.commit()

        mock_fh.return_value = "f" * 64
        mock_ah.return_value = (shared, None)

        SentinelStage().execute(ctx)

        dupes = ctx.conn.execute(
            "SELECT COUNT(*) c FROM events WHERE event_type = 'DUPLICATE_FOUND'"
        ).fetchone()["c"]
        assert dupes == 0, "matched against a phantom"

    @patch("musaeus.stages.sentinel.audio_hash_safe")
    @patch("musaeus.stages.sentinel.file_hash")
    def test_a_quarantined_row_is_not_matched_though_its_file_exists(
        self, mock_fh, mock_ah, ctx, tmp_path
    ):
        """The case the STATUS filter exists for, distinct from liveness.

        A quarantined file is still on disk -- it sits in QUARANTINE/ -- so a
        liveness check alone happily matches against it. It is not a library
        member, and an incoming file is not its duplicate.
        """
        shared = "d" * 64
        quarantined = tmp_path / "QUARANTINE" / "denied.flac"
        quarantined.parent.mkdir(parents=True, exist_ok=True)
        quarantined.write_bytes(b"AUDIO")
        ctx.conn.execute(
            "INSERT INTO archive (file_path, audio_hash, status) "
            "VALUES (?, ?, 'QUARANTINED')",
            (str(quarantined), shared),
        )
        arrival = tmp_path / "new3.flac"
        arrival.write_bytes(b"AUDIO")
        _insert_pending(ctx, str(arrival))
        ctx.conn.commit()

        mock_fh.return_value = "f" * 64
        mock_ah.return_value = (shared, None)

        SentinelStage().execute(ctx)

        dupes = ctx.conn.execute(
            "SELECT COUNT(*) c FROM events WHERE event_type = 'DUPLICATE_FOUND'"
        ).fetchone()["c"]
        assert dupes == 0, "matched against a quarantined row that is still on disk"

    @patch("musaeus.stages.sentinel.audio_hash_safe")
    @patch("musaeus.stages.sentinel.file_hash")
    def test_a_row_whose_file_vanished_is_not_matched_either(
        self, mock_fh, mock_ah, ctx, tmp_path
    ):
        """Status says live, disk says gone. The disk is the true answer."""
        shared = "b" * 64
        ctx.conn.execute(
            "INSERT INTO archive (file_path, audio_hash, status) VALUES (?, ?, 'CATALOGUED')",
            (str(tmp_path / "not-really-there.flac"), shared),
        )
        arrival = tmp_path / "new2.flac"
        arrival.write_bytes(b"AUDIO")
        _insert_pending(ctx, str(arrival))
        ctx.conn.commit()

        mock_fh.return_value = "f" * 64
        mock_ah.return_value = (shared, None)

        SentinelStage().execute(ctx)

        dupes = ctx.conn.execute(
            "SELECT COUNT(*) c FROM events WHERE event_type = 'DUPLICATE_FOUND'"
        ).fetchone()["c"]
        assert dupes == 0, "status was trusted over the filesystem"

    @patch("musaeus.stages.sentinel.audio_hash_safe")
    @patch("musaeus.stages.sentinel.file_hash")
    def test_a_genuine_live_duplicate_is_still_caught(
        self, mock_fh, mock_ah, ctx, tmp_path
    ):
        """The guard must not suppress real duplicates."""
        shared = "c" * 64
        original = tmp_path / "original.flac"
        original.write_bytes(b"AUDIO")
        ctx.conn.execute(
            "INSERT INTO archive (file_path, audio_hash, status) VALUES (?, ?, 'CATALOGUED')",
            (str(original), shared),
        )
        arrival = tmp_path / "copy.flac"
        arrival.write_bytes(b"AUDIO")
        _insert_pending(ctx, str(arrival))
        ctx.conn.commit()

        mock_fh.return_value = "f" * 64
        mock_ah.return_value = (shared, None)

        SentinelStage().execute(ctx)

        dupes = ctx.conn.execute(
            "SELECT COUNT(*) c FROM events WHERE event_type = 'DUPLICATE_FOUND'"
        ).fetchone()["c"]
        assert dupes >= 1, "a real duplicate against a live file must still be found"


# ── re-hashing must not demote a row ─────────────────────────────────────────
#
# Sentinel wrote status='HASHED' unconditionally. Hashing a file that was
# already CATALOGUED and finalized therefore knocked it BACKWARDS out of the
# status every later stage selects on -- Tagger, Organize and Audit all read
# status='CATALOGUED' -- while making it eligible for the whole downstream
# chain again.
#
# Measured 2026-08-30: a re-hash pass over the library demoted 1,100
# finalized rows before it was stopped. It would have taken all 9,556.


class TestReHashingDoesNotDemote:
    @patch("musaeus.stages.sentinel.audio_hash_safe")
    @patch("musaeus.stages.sentinel.file_hash")
    def test_a_finalized_catalogued_row_keeps_its_status(
        self, mock_fh, mock_ah, ctx, tmp_path
    ):
        track = tmp_path / "finalized.flac"
        track.write_bytes(b"AUDIO")
        ctx.conn.execute(
            "INSERT INTO archive (file_path, status, finalized_at) "
            "VALUES (?, 'CATALOGUED', '2026-08-27 00:00:00')",
            (str(track),),
        )
        ctx.conn.commit()
        mock_fh.return_value = "f" * 64
        mock_ah.return_value = ("a" * 64, None)

        SentinelStage().execute(ctx)

        row = ctx.conn.execute(
            "SELECT status, audio_hash FROM archive WHERE file_path = ?", (str(track),)
        ).fetchone()
        assert row["status"] == "CATALOGUED", "re-hashing demoted a finalized row"
        assert row["audio_hash"] == "a" * 64, "the hash must still be updated"

    @patch("musaeus.stages.sentinel.audio_hash_safe")
    @patch("musaeus.stages.sentinel.file_hash")
    def test_a_pending_row_still_advances_to_hashed(self, mock_fh, mock_ah, ctx, tmp_path):
        """The guard must not stop a new file progressing."""
        track = tmp_path / "new.flac"
        track.write_bytes(b"AUDIO")
        _insert_pending(ctx, str(track))
        mock_fh.return_value = "f" * 64
        mock_ah.return_value = ("b" * 64, None)

        SentinelStage().execute(ctx)

        row = ctx.conn.execute(
            "SELECT status FROM archive WHERE file_path = ?", (str(track),)
        ).fetchone()
        assert row["status"] == "HASHED"

    @patch("musaeus.stages.sentinel.audio_hash_safe")
    @patch("musaeus.stages.sentinel.file_hash")
    def test_a_ghost_whose_file_returns_recovers(self, mock_fh, mock_ah, ctx, tmp_path):
        """The anti-demotion guard must not make GHOST a one-way door.

        GhostStage only ever SETS ghost; nothing else clears it, and the only
        other reset is the console's manual soft reset. So an unmounted drive
        that comes back left every row permanently invisible to Scholar,
        Canonicalize, Tagger, Organize and Audit -- which is precisely the
        case the GHOST design exists to survive.
        """
        back = tmp_path / "returned.flac"
        back.write_bytes(b"AUDIO")
        ctx.conn.execute(
            "INSERT INTO archive (file_path, status, audio_hash) VALUES (?, 'GHOST', NULL)",
            (str(back),),
        )
        ctx.conn.commit()
        mock_fh.return_value = "f" * 64
        mock_ah.return_value = ("a" * 64, None)

        SentinelStage().execute(ctx)

        row = ctx.conn.execute(
            "SELECT status FROM archive WHERE file_path = ?", (str(back),)
        ).fetchone()
        assert row["status"] == "HASHED", "a returning GHOST never recovers"


class TestSentinelWantedList:
    """A decode failure is the only unambiguous "unplayable" signal the
    pipeline produces. It used to stop at a log line."""

    def _probe_stub(self, artist, title, album=""):
        return {
            "format": {"tags": {"artist": artist, "title": title, "album": album}},
            "streams": [{"codec_type": "audio", "codec_name": "alac"}],
        }

    def test_an_undecodable_file_lands_on_the_wanted_list(self, ctx, tmp_path):
        bad = tmp_path / "INBOX" / "Aerosmith - What It Takes.m4a"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"not really audio")
        _insert_pending(ctx, str(bad))

        with patch("musaeus.stages.sentinel.audio_hash_safe",
                   return_value=(None, "ffmpeg exited 69: invalid element channel count")), \
             patch("musaeus.stages.sentinel.file_hash", return_value="f" * 64), \
             patch("musaeus.stages.scholar._probe",
                   return_value=self._probe_stub("Aerosmith", "What It Takes", "Big Ones")):
            SentinelStage().execute(ctx)

        rows = list(csv.reader(ctx.config.tunemymusic_csv_path.open(encoding="utf-8")))
        assert rows[0] == ["Title", "Artist", "Album"]
        assert ["What It Takes", "Aerosmith", "Big Ones"] in rows[1:]

    def test_a_copy_already_catalogued_is_not_put_on_the_buy_list(self, ctx, tmp_path):
        """Bowie's "Cat People" failed to decode in one copy while a clean
        53MB master sat in the library. Asking Grey to re-buy what he owns
        is how a useful list becomes one he stops reading."""
        upsert_archive(ctx.conn, {
            "file_path": str(tmp_path / "good.m4a"), "status": "CATALOGUED",
            "artist": "David Bowie", "title": "Cat People",
        })
        ctx.conn.commit()

        bad = tmp_path / "INBOX" / "David Bowie - Cat People.m4a"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"truncated")
        _insert_pending(ctx, str(bad))

        with patch("musaeus.stages.sentinel.audio_hash_safe", return_value=(None, "partial file")), \
             patch("musaeus.stages.sentinel.file_hash", return_value="e" * 64), \
             patch("musaeus.stages.scholar._probe",
                   return_value=self._probe_stub("David Bowie", "Cat People")):
            SentinelStage().execute(ctx)

        assert not ctx.config.tunemymusic_csv_path.exists(), (
            "a record already in the library must not be added to the buy list"
        )

    def test_a_wanted_list_failure_never_fails_the_hash_pass(self, ctx, tmp_path):
        """Bookkeeping must not cost a run its hashing."""
        bad = tmp_path / "INBOX" / "x.m4a"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"bad")
        _insert_pending(ctx, str(bad))

        with patch("musaeus.stages.sentinel.audio_hash_safe", return_value=(None, "boom")), \
             patch("musaeus.stages.sentinel.file_hash", return_value="d" * 64), \
             patch("musaeus.stages.sentinel._want_replacement",
                   side_effect=RuntimeError("disk on fire")):
            result = SentinelStage().execute(ctx)

        assert result.files_errored == 1  # the hash failure is still counted
