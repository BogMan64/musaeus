"""
Tests for NearDupeStage — metadata-based near-duplicate detection.

Requires rapidfuzz; tests skip if not installed.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.neardupe import NearDupeStage, _normalise, _group_id

# Skip all tests if rapidfuzz is not available
rapidfuzz = pytest.importorskip("rapidfuzz")


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
    # Create MetaData dir with an empty artist_canon.tsv
    meta_dir = tmp_path / "MetaData"
    meta_dir.mkdir()
    (meta_dir / "artist_canon.tsv").write_text("", encoding="utf-8")
    return MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=meta_dir,
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


def _insert_catalogued(ctx: RunContext, file_path: str, artist: str, title: str) -> None:
    upsert_archive(ctx.conn, {
        "file_path": file_path,
        "status": "CATALOGUED",
        "artist": artist,
        "title": title,
        "bitrate": 320000,
        "size_bytes": 5000000,
    })
    ctx.conn.commit()


# ── _normalise helper ─────────────────────────────────────────────────────────

class TestNormalise:
    def test_lowercase(self):
        assert _normalise("HELLO") == "hello"

    def test_strip_punctuation(self):
        assert _normalise("rock'n'roll") == "rock n roll"

    def test_collapse_whitespace(self):
        assert _normalise("  hello   world  ") == "hello world"

    def test_strip_leading_the(self):
        assert _normalise("The Beatles") == "beatles"

    def test_unicode_nfd(self):
        # Accented characters normalised
        result = _normalise("cafe\u0301")  # café with combining accent
        assert "cafe" in result


# ── _group_id helper ──────────────────────────────────────────────────────────

class TestGroupId:
    def test_stable(self):
        gid1 = _group_id("/a.flac", "/b.flac")
        gid2 = _group_id("/a.flac", "/b.flac")
        assert gid1 == gid2

    def test_order_independent(self):
        gid1 = _group_id("/a.flac", "/b.flac")
        gid2 = _group_id("/b.flac", "/a.flac")
        assert gid1 == gid2

    def test_prefix(self):
        gid = _group_id("/x.flac", "/y.flac")
        assert gid.startswith("near_")


# ── Validate ──────────────────────────────────────────────────────────────────

class TestNearDupeValidate:
    @patch("musaeus.stages.neardupe.get_config")
    def test_validate_passes_with_rapidfuzz(self, mock_cfg, ctx, cfg):
        mock_cfg.return_value = cfg
        NearDupeStage().validate(ctx)


# ── Dry run ───────────────────────────────────────────────────────────────────

class TestNearDupeDryRun:
    @patch("musaeus.stages.neardupe.get_config")
    def test_dry_run_detects_near_dupes(self, mock_cfg, ctx_dry, cfg, tmp_path):
        mock_cfg.return_value = cfg
        # Two tracks, same artist, very similar titles
        _insert_catalogued(ctx_dry, str(tmp_path / "a.flac"), "The Beatles", "Yesterday")
        _insert_catalogued(ctx_dry, str(tmp_path / "b.flac"), "The Beatles", "Yesterday (Remaster)")

        result = NearDupeStage().execute(ctx_dry)
        assert result.dry_run is True
        # Should detect the near-dupe pair
        assert result.files_changed >= 1

    @patch("musaeus.stages.neardupe.get_config")
    def test_dry_run_no_db_writes(self, mock_cfg, ctx_dry, cfg, tmp_path):
        mock_cfg.return_value = cfg
        _insert_catalogued(ctx_dry, str(tmp_path / "a.flac"), "Artist", "Song A")
        _insert_catalogued(ctx_dry, str(tmp_path / "b.flac"), "Artist", "Song A Remix")

        NearDupeStage().execute(ctx_dry)

        count = ctx_dry.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0]
        assert count == 0

    @patch("musaeus.stages.neardupe.get_config")
    def test_dry_run_no_matches(self, mock_cfg, ctx_dry, cfg, tmp_path):
        mock_cfg.return_value = cfg
        _insert_catalogued(ctx_dry, str(tmp_path / "a.flac"), "Artist A", "Totally Different")
        _insert_catalogued(ctx_dry, str(tmp_path / "b.flac"), "Artist B", "Completely Other")

        result = NearDupeStage().execute(ctx_dry)
        assert result.files_changed == 0
        assert any("No new near-duplicates" in n for n in result.notes)


# ── Run ───────────────────────────────────────────────────────────────────────

class TestNearDupeRun:
    @patch("musaeus.stages.neardupe.get_config")
    def test_run_writes_to_duplicates_table(self, mock_cfg, ctx, cfg, tmp_path):
        mock_cfg.return_value = cfg
        _insert_catalogued(ctx, str(tmp_path / "a.flac"), "Pink Floyd", "Comfortably Numb")
        _insert_catalogued(ctx, str(tmp_path / "b.flac"), "Pink Floyd", "Comfortably Numb (Live)")

        result = NearDupeStage().execute(ctx)
        assert result.files_changed >= 1

        dupes = ctx.conn.execute(
            "SELECT * FROM duplicates WHERE duplicate_type='NEAR'"
        ).fetchall()
        assert len(dupes) >= 2  # Both files in the group

    @patch("musaeus.stages.neardupe.get_config")
    def test_run_different_artists_not_matched(self, mock_cfg, ctx, cfg, tmp_path):
        mock_cfg.return_value = cfg
        _insert_catalogued(ctx, str(tmp_path / "a.flac"), "Artist A", "Same Title")
        _insert_catalogued(ctx, str(tmp_path / "b.flac"), "Artist B", "Same Title")

        result = NearDupeStage().execute(ctx)
        # Different artists → no match (unless artists are very similar)
        dupes = ctx.conn.execute(
            "SELECT * FROM duplicates WHERE duplicate_type='NEAR'"
        ).fetchall()
        assert len(dupes) == 0

    @patch("musaeus.stages.neardupe.get_config")
    def test_run_idempotent(self, mock_cfg, ctx, cfg, tmp_path):
        mock_cfg.return_value = cfg
        _insert_catalogued(ctx, str(tmp_path / "a.flac"), "Artist", "Song Title")
        _insert_catalogued(ctx, str(tmp_path / "b.flac"), "Artist", "Song Title Remix")

        NearDupeStage().execute(ctx)
        count1 = ctx.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0]

        # Run again — should not create duplicates
        NearDupeStage().execute(ctx)
        count2 = ctx.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0]
        assert count2 == count1

    @patch("musaeus.stages.neardupe.get_config")
    def test_run_skips_exact_dupes(self, mock_cfg, ctx, cfg, tmp_path):
        mock_cfg.return_value = cfg
        # Pre-insert an EXACT duplicate
        _insert_catalogued(ctx, str(tmp_path / "a.flac"), "Artist", "Song")
        _insert_catalogued(ctx, str(tmp_path / "b.flac"), "Artist", "Song (same)")
        ctx.conn.execute(
            "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, run_id) VALUES (?, ?, 'EXACT', 1.0, ?)",
            ("dup_exact", str(tmp_path / "a.flac"), ctx.run_id),
        )
        ctx.conn.commit()

        result = NearDupeStage().execute(ctx)
        # a.flac is in exact duplicates → should be skipped in neardupe
        # The pair should not be flagged since a.flac is already EXACT
        near_dupes = ctx.conn.execute(
            "SELECT * FROM duplicates WHERE duplicate_type='NEAR'"
        ).fetchall()
        assert len(near_dupes) == 0

    @patch("musaeus.stages.neardupe.get_config")
    def test_run_empty_archive(self, mock_cfg, ctx, cfg):
        mock_cfg.return_value = cfg
        result = NearDupeStage().execute(ctx)
        assert result.files_processed == 0
        assert result.files_changed == 0
