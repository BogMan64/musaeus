"""
Tests for VariousArtistsFixStage — resolve the real artist for
"Various Artists" tagged rows and relocate the file. Standalone stage,
not part of DEFAULT_PIPELINE.

MusicBrainz lookups are mocked (network) except pattern-matching tests,
which never call it at all -- bracket/filename-segment strategies are
checked first and short-circuit before any network call.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.various_artists_fix import (
    VariousArtistsFixStage,
    extract_from_brackets,
    extract_from_filename_segments,
    find_real_artist,
    is_various,
)

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


def _make_row(ctx: RunContext, relpath: str, artist: str, title: str, album: str = "") -> Path:
    path = ctx.alac_library / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"FAKE AUDIO")
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(path),
            "status": "CATALOGUED",
            "artist": artist,
            "title": title,
            "album": album,
        },
    )
    ctx.conn.commit()
    return path


# ── Pure helpers ──────────────────────────────────────────────────────────────


class TestIsVarious:
    @pytest.mark.parametrize("v", ["Various Artists", "various", "VA", "Various Artist"])
    def test_recognized_forms(self, v):
        assert is_various(v) is True

    def test_real_artist_not_various(self):
        assert is_various("The Beatles") is False


class TestExtractFromBrackets:
    def test_simple_bracket(self):
        assert extract_from_brackets("Song Title [Eric Carmen].m4a") == "Eric Carmen"

    def test_bracket_with_dash_keeps_first_part(self):
        assert extract_from_brackets("Song Title [The Ronettes-Phil Spector].m4a") == "The Ronettes"

    def test_no_bracket_returns_empty(self):
        assert extract_from_brackets("Song Title.m4a") == ""


class TestExtractFromFilenameSegments:
    def test_real_vault_pattern(self):
        """Confirmed real case from the vault: 'Various Artists - Eric
        Carmen - All By Myself.m4a'."""
        name = "Various Artists - Eric Carmen - All By Myself.m4a"
        assert extract_from_filename_segments(name) == "Eric Carmen"

    def test_too_few_segments_returns_empty(self):
        assert extract_from_filename_segments("Various Artists - Song.m4a") == ""

    def test_first_segment_not_various_returns_empty(self):
        name = "The Beatles - Abbey Road - Come Together.m4a"
        assert extract_from_filename_segments(name) == ""


class TestFindRealArtist:
    def test_bracket_wins_over_musicbrainz(self, tmp_path):
        source = tmp_path / "Song [Eric Carmen].m4a"
        with patch("musaeus.stages.various_artists_fix._lookup_musicbrainz") as m_mb:
            artist, strategy = find_real_artist(source, "Song", "", use_mb=True)
        assert artist == "Eric Carmen"
        assert strategy == "bracket"
        m_mb.assert_not_called()

    def test_filename_segment_used_when_no_bracket(self, tmp_path):
        source = tmp_path / "Various Artists - Eric Carmen - All By Myself.m4a"
        with patch("musaeus.stages.various_artists_fix._lookup_musicbrainz") as m_mb:
            artist, strategy = find_real_artist(source, "All By Myself", "", use_mb=True)
        assert artist == "Eric Carmen"
        assert strategy == "filename_segment"
        m_mb.assert_not_called()

    def test_falls_back_to_musicbrainz(self, tmp_path):
        source = tmp_path / "Various Artists - Some Song.m4a"
        with (
            patch(
                "musaeus.stages.various_artists_fix._lookup_musicbrainz",
                return_value="Real Artist",
            ),
            patch("musaeus.stages.various_artists_fix.time.sleep"),
        ):
            artist, strategy = find_real_artist(source, "Some Song", "", use_mb=True)
        assert artist == "Real Artist"
        assert strategy == "musicbrainz"

    def test_no_mb_skips_lookup(self, tmp_path):
        source = tmp_path / "Various Artists - Some Song.m4a"
        with patch("musaeus.stages.various_artists_fix._lookup_musicbrainz") as m_mb:
            artist, strategy = find_real_artist(source, "Some Song", "", use_mb=False)
        assert artist == ""
        assert strategy == "unknown"
        m_mb.assert_not_called()

    def test_unresolvable_returns_unknown(self, tmp_path):
        source = tmp_path / "Various Artists - Some Song.m4a"
        with patch("musaeus.stages.various_artists_fix._lookup_musicbrainz", return_value=""):
            artist, strategy = find_real_artist(source, "Some Song", "", use_mb=True)
        assert artist == ""
        assert strategy == "unknown"


# ── Stage run ──────────────────────────────────────────────────────────────────


class TestVariousArtistsFixRun:
    def test_resolved_row_moved_and_artist_corrected(self, ctx):
        path = _make_row(
            ctx,
            f"Various Artists/Unsorted/{_TEST_BATCH_DATE.replace('-', '')}.m4a",
            "Various Artists",
            "All By Myself",
        )
        # Rename on disk to match the real-vault filename pattern the
        # filename-segment strategy targets.
        real_path = path.with_name("Various Artists - Eric Carmen - All By Myself.m4a")
        path.rename(real_path)
        ctx.conn.execute(
            "UPDATE archive SET file_path = ? WHERE file_path = ?", (str(real_path), str(path))
        )
        ctx.conn.commit()

        result = VariousArtistsFixStage().run(ctx)

        assert result.success is True
        assert result.files_changed == 1
        assert not real_path.exists()

        row = ctx.conn.execute(
            "SELECT artist, file_path FROM archive WHERE title = 'All By Myself'"
        ).fetchone()
        assert row["artist"] == "Eric Carmen"
        assert "Eric Carmen" in row["file_path"]
        assert Path(row["file_path"]).exists()

    def test_unresolvable_row_left_untouched(self, ctx):
        path = _make_row(
            ctx,
            "Various Artists/Unsorted/mystery.m4a",
            "Various Artists",
            "Totally Unknown Song",
        )

        with patch("musaeus.stages.various_artists_fix._lookup_musicbrainz", return_value=""):
            result = VariousArtistsFixStage().run(ctx)

        assert result.files_changed == 0
        assert path.exists()
        row = ctx.conn.execute(
            "SELECT artist, status FROM archive WHERE title = 'Totally Unknown Song'"
        ).fetchone()
        assert row["artist"] == "Various Artists"
        assert row["status"] == "CATALOGUED"

    def test_non_various_artist_row_untouched(self, ctx):
        path = _make_row(ctx, "Beatles/Help/song.m4a", "The Beatles", "Yesterday")

        result = VariousArtistsFixStage().run(ctx)

        assert result.files_processed == 0
        assert path.exists()

    def test_missing_file_reported_not_crash(self, ctx):
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(ctx.alac_library / "Various Artists/gone.m4a"),
                "status": "CATALOGUED",
                "artist": "Various Artists",
                "title": "Song",
            },
        )
        ctx.conn.commit()

        result = VariousArtistsFixStage().run(ctx)

        assert result.files_errored == 1
        assert any("missing on disk" in e for e in result.errors)

    def test_dry_run_makes_no_changes(self, ctx):
        path = path = _make_row(
            ctx,
            "Various Artists/Unsorted/song.m4a",
            "Various Artists",
            "All By Myself",
        )
        real_path = path.with_name("Various Artists - Eric Carmen - All By Myself.m4a")
        path.rename(real_path)
        ctx.conn.execute(
            "UPDATE archive SET file_path = ? WHERE file_path = ?", (str(real_path), str(path))
        )
        ctx.conn.commit()

        result = VariousArtistsFixStage().dry_run(ctx)

        assert result.files_processed == 1
        assert real_path.exists()
        row = ctx.conn.execute(
            "SELECT artist FROM archive WHERE title = 'All By Myself'"
        ).fetchone()
        assert row["artist"] == "Various Artists"
