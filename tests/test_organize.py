"""
Tests for OrganizeStage — rename/move CATALOGUED files into Artist/Album/.

Uses real files on disk (not mocked) because this stage's core correctness
claim is specifically about disk/DB staying in sync through a real
rename+DB-update sequence -- a prior bug here (row["rowid"] raising
IndexError on every live call, since archive.id is an INTEGER PRIMARY
KEY and SQLite exposes it under its real column name, not "rowid", once
selected alongside other columns) went undetected because no test here
exercised the actual live rename path end-to-end.
"""

from pathlib import Path

import pytest
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.organize import (
    OrganizeStage,
    build_track_filename,
    sanitize_path_component,
    strip_track_number_prefix,
    unique_path,
)


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
    cfg.inbox.mkdir(parents=True, exist_ok=True)
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=False)


@pytest.fixture
def ctx_dry(cfg: MusicConfig) -> RunContext:
    cfg.inbox.mkdir(parents=True, exist_ok=True)
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=True)


def _make_track(ctx: RunContext, relpath: str, artist: str, album: str, title: str) -> Path:
    """Create a real (empty) file under INBOX and register it as CATALOGUED."""
    path = ctx.inbox / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"FAKE AUDIO DATA")
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(path),
            "status": "CATALOGUED",
            "artist": artist,
            "album": album,
            "title": title,
        },
    )
    ctx.conn.commit()
    return path


# ── Naming helpers ────────────────────────────────────────────────────────────

class TestNamingHelpers:
    def test_strip_track_number_prefix(self):
        assert strip_track_number_prefix("171. Song Title") == "Song Title"
        assert strip_track_number_prefix("01 - Title") == "Title"
        assert strip_track_number_prefix("No Prefix Here") == "No Prefix Here"

    def test_sanitize_path_component_forbidden_chars(self):
        result = sanitize_path_component('AC/DC: Greatest?')
        assert "/" not in result
        assert ":" not in result
        assert "?" not in result

    def test_build_track_filename(self):
        assert build_track_filename("The Beatles", "Come Together", ".m4a") == "The Beatles - Come Together.m4a"

    def test_unique_path_no_collision(self, tmp_path):
        target = tmp_path / "file.m4a"
        assert unique_path(target) == target

    def test_unique_path_with_collision(self, tmp_path):
        target = tmp_path / "file.m4a"
        target.write_bytes(b"x")
        result = unique_path(target)
        assert result == tmp_path / "file (2).m4a"


# ── Live run: the actual regression this file guards against ────────────────

class TestOrganizeRunLive:
    def test_move_to_artist_album_folder(self, ctx):
        """
        The core regression test: a real file, a real CATALOGUED row, a
        real (not dry-run) OrganizeStage.run() call. Before the fix, this
        raised IndexError inside _apply_rename() the moment it tried to
        read row["rowid"] -- every live organize run crashed on the first
        file that needed a move, silently reverting nothing (the crash
        happened before any disk write in that particular bug, but the
        point of this test is that the stage must complete, not just not
        corrupt anything).
        """
        track = _make_track(ctx, "flat_name.m4a", "Test Artist", "Test Album", "Song One")

        result = OrganizeStage().execute(ctx)

        assert result.success is True
        assert result.files_changed == 1

        expected = ctx.inbox / "Test Artist" / "Test Album" / "Test Artist - Song One.m4a"
        assert expected.exists()
        assert not track.exists()

        # DB must point at the new location, not the old one.
        row = ctx.conn.execute(
            "SELECT file_path FROM archive WHERE artist='Test Artist' AND title='Song One'"
        ).fetchone()
        assert row["file_path"] == str(expected)

    def test_move_multiple_files_same_run(self, ctx):
        """Two files needing a move in the same run -- both must succeed,
        confirming the id lookup works across more than one row, not just
        coincidentally for a single-row table."""
        _make_track(ctx, "a.m4a", "Artist A", "Album A", "Title A")
        _make_track(ctx, "b.m4a", "Artist B", "Album B", "Title B")

        result = OrganizeStage().execute(ctx)

        assert result.success is True
        assert result.files_changed == 2
        assert (ctx.inbox / "Artist A" / "Album A" / "Artist A - Title A.m4a").exists()
        assert (ctx.inbox / "Artist B" / "Album B" / "Artist B - Title B.m4a").exists()

    def test_rename_only_same_directory(self, ctx):
        """When the target dir already matches, this is a pure rename
        (current_path.parent == target_path.parent branch), not a move --
        exercises the other _apply_rename call site."""
        target_dir = ctx.inbox / "Test Artist" / "Test Album"
        target_dir.mkdir(parents=True)
        path = target_dir / "wrong_name.m4a"
        path.write_bytes(b"DATA")
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(path),
                "status": "CATALOGUED",
                "artist": "Test Artist",
                "album": "Test Album",
                "title": "Right Name",
            },
        )
        ctx.conn.commit()

        result = OrganizeStage().execute(ctx)

        assert result.success is True
        expected = target_dir / "Test Artist - Right Name.m4a"
        assert expected.exists()
        assert not path.exists()

    def test_already_organized_file_skipped(self, ctx):
        """A file already at its correct final path is a no-op, not an
        error and not a redundant move."""
        target_dir = ctx.inbox / "Test Artist" / "Test Album"
        target_dir.mkdir(parents=True)
        path = target_dir / "Test Artist - Already Right.m4a"
        path.write_bytes(b"DATA")
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(path),
                "status": "CATALOGUED",
                "artist": "Test Artist",
                "album": "Test Album",
                "title": "Already Right",
            },
        )
        ctx.conn.commit()

        result = OrganizeStage().execute(ctx)

        assert result.success is True
        assert result.files_changed == 0
        assert path.exists()

    def test_missing_file_reported_as_error_not_crash(self, ctx):
        """A CATALOGUED row whose file vanished from disk must be reported
        via files_errored/result.errors, not crash the stage."""
        missing_path = ctx.inbox / "gone.m4a"
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(missing_path),
                "status": "CATALOGUED",
                "artist": "Ghost Artist",
                "album": "Ghost Album",
                "title": "Ghost Title",
            },
        )
        ctx.conn.commit()

        result = OrganizeStage().execute(ctx)

        assert result.files_errored == 1
        assert any("missing" in e.lower() for e in result.errors)


# ── Dry run ───────────────────────────────────────────────────────────────────

class TestOrganizeDryRun:
    def test_dry_run_does_not_move_file(self, ctx_dry):
        track = _make_track(ctx_dry, "flat_name.m4a", "Test Artist", "Test Album", "Song One")

        result = OrganizeStage().execute(ctx_dry)

        assert result.dry_run is True
        assert result.files_changed == 1
        # Nothing should have actually moved.
        assert track.exists()
        expected = ctx_dry.inbox / "Test Artist" / "Test Album" / "Test Artist - Song One.m4a"
        assert not expected.exists()

        row = ctx_dry.conn.execute(
            "SELECT file_path FROM archive WHERE title='Song One'"
        ).fetchone()
        assert row["file_path"] == str(track)
