"""The second review of PR #36 (2026-09-25): places that still assumed the old
two-tier layout, or disagreed about a collision name.

One test per finding, each built the way the review reproduced it. The retired
bake script (finding 2) and the conftest switches (finding 4) are tested in
test_build_alac_library_lufs_bake.py and test_tests_cannot_reach_a_real_tier.py.
"""

from __future__ import annotations

from pathlib import Path

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.audit import AuditStage
from musaeus.stages.finalize import FinalizeStage
from musaeus.stages.organize import OrganizeStage, library_relpath
from musaeus.stages.various_artists_fix import VariousArtistsFixStage


def _ctx(tmp_path: Path) -> RunContext:
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        alac_archive=tmp_path / "Libraries" / "ALAC-Archival",
        db_path=tmp_path / "musaeus.db",
    )
    cfg.ensure_dirs()
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _row(ctx, path: Path, artist, title, genre="Rock", data=b"x", finalized=False, h=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(path),
            "status": "CATALOGUED",
            "artist": artist,
            "album": "Album",
            "title": title,
            "genre": genre,
            "audio_hash": h or path.name,
        },
    )
    ctx.conn.execute(
        "UPDATE archive SET canonicalized_at = datetime('now'), canon_action = 'PASSTHROUGH'"
        + (", finalized_at = datetime('now')" if finalized else "")
        + " WHERE file_path = ?",
        (str(path),),
    )
    ctx.conn.commit()


def _paths(ctx):
    return {r[0]: r[1] for r in ctx.conn.execute("SELECT id, file_path FROM archive")}


def test_1_a_forced_finalize_never_pulls_a_library_file_into_the_masters_tier(tmp_path):
    ctx = _ctx(tmp_path)
    rel = library_relpath("Dion", None, "Rock", "Album", "Song", ".m4a")
    (ctx.config.alac_archive / rel).parent.mkdir(parents=True, exist_ok=True)
    (ctx.config.alac_archive / rel).write_bytes(b"master")
    _row(ctx, ctx.alac_library / rel, "Dion", "Song", data=b"-18 copy", finalized=True)
    ctx.set("finalize_force", True)
    before = _paths(ctx)
    FinalizeStage().execute(ctx)
    assert _paths(ctx) == before, "a library file was pulled into ALAC-Archival"
    assert (ctx.alac_library / rel).read_bytes() == b"-18 copy"
    assert sorted(p.name for p in ctx.config.alac_archive.rglob("*.m4a")) == ["Dion - Song.m4a"]


def test_3_the_various_artists_fix_keeps_a_master_in_the_masters_tier(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.set("various_artists_no_mb", True)
    src = (
        ctx.config.alac_archive
        / "Rock"
        / "Various Artists"
        / "Album"
        / "Various Artists - Dion - Runaround Sue.m4a"
    )
    _row(ctx, src, "Various Artists", "Dion - Runaround Sue", finalized=True)
    VariousArtistsFixStage().execute(ctx)
    (fp,) = ctx.conn.execute("SELECT file_path FROM archive").fetchone()
    fp = Path(fp)
    assert fp.is_relative_to(ctx.config.alac_archive), f"master moved out of the masters tier: {fp}"
    assert fp.relative_to(ctx.config.alac_archive).parts[:2] == ("Rock", "Dion"), (
        "not filed by the shared rule"
    )
    assert fp.is_file() and not any(ctx.alac_library.rglob("*.m4a"))


def test_3b_a_new_arrival_is_corrected_in_place_not_moved_into_the_library(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.set("various_artists_no_mb", True)
    src = ctx.inbox / "Various Artists - Dion - Runaround Sue.m4a"
    _row(ctx, src, "Various Artists", "Dion - Runaround Sue")
    VariousArtistsFixStage().execute(ctx)
    (fp, artist) = ctx.conn.execute("SELECT file_path, artist FROM archive").fetchone()
    assert Path(fp) == src and src.is_file(), "an unfiled arrival was moved before Finalize saw it"
    assert artist == "Dion"


def test_5_a_forced_finalize_does_not_flip_a_collision_named_master(tmp_path):
    ctx = _ctx(tmp_path)
    _row(ctx, ctx.inbox / "a.m4a", "Dion", "Song", data=b"one")
    _row(ctx, ctx.inbox / "b.m4a", "Dion", "Song", data=b"two")
    FinalizeStage().execute(ctx)
    before = _paths(ctx)
    ctx.set("finalize_force", True)
    for run in range(3):
        FinalizeStage().execute(ctx)
        assert _paths(ctx) == before, f"renamed on forced run {run + 1}"


def test_6_an_orphaned_master_fails_the_audit(tmp_path):
    ctx = _ctx(tmp_path)
    orphan = ctx.config.alac_archive / "Rock" / "Dion" / "Album" / "Dion - Nobody's Row.m4a"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"x")
    result = AuditStage().execute(ctx)
    assert any("no matching finalized row" in e and orphan.name in e for e in result.errors)


def test_7_a_real_number_in_an_old_title_is_not_taken_for_a_collision_suffix(tmp_path):
    ctx = _ctx(tmp_path)
    _row(ctx, ctx.inbox / "a.m4a", "Dion", "Song", data=b"one")
    _row(ctx, ctx.inbox / "b.m4a", "Dion", "Song (1999)", data=b"two")
    FinalizeStage().execute(ctx)
    ctx.conn.execute("UPDATE archive SET title = 'Song' WHERE title = 'Song (1999)'")
    ctx.conn.commit()
    OrganizeStage().execute(ctx)
    names = sorted(Path(p).name for p in _paths(ctx).values())
    assert names == ["Dion - Song (2).m4a", "Dion - Song.m4a"], names
