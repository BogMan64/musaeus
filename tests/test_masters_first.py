"""A run ends with the master in ALAC-Archival, and the catalogue row points at it.

Grey, 2026-09-25, after a review of the two-file design (row on the -18 LUFS
copy, master found at a mirrored path) found five ways the pair drifted
apart: one file per track. The -18 LUFS ALAC_Library is an edition, built
from the masters and disposable. These hold Finalize and Organize to that:
masters filed once, refiled by Organize when their metadata changes, and
never flip-flopping between " (2)" and " (3)".
"""

from __future__ import annotations

from pathlib import Path

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.finalize import FinalizeStage
from musaeus.stages.organize import OrganizeStage


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


def _arrive(ctx, name, artist, title, genre, data):
    p = ctx.inbox / name
    p.write_bytes(data)
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(p),
            "status": "CATALOGUED",
            "artist": artist,
            "album": "Album",
            "title": title,
            "genre": genre,
            "audio_hash": name,
        },
    )
    ctx.conn.execute(
        "UPDATE archive SET canonicalized_at = datetime('now'), canon_action = 'PASSTHROUGH' WHERE file_path = ?",
        (str(p),),
    )
    ctx.conn.commit()


def _paths(ctx):
    return {r[0]: r[1] for r in ctx.conn.execute("SELECT id, file_path FROM archive")}


def test_finalize_files_the_master_and_the_row_points_at_it(tmp_path):
    ctx = _ctx(tmp_path)
    _arrive(ctx, "a.m4a", "The Black Eyed Peas", "Hey Mama", "Hip Hop", b"one")
    assert FinalizeStage().execute(ctx).success
    (fp,) = ctx.conn.execute("SELECT file_path FROM archive").fetchone()
    rel = Path(fp).relative_to(ctx.config.alac_archive)
    assert str(rel) == "Hip Hop/Black Eyed Peas, The/Album/Black Eyed Peas, The - Hey Mama.m4a"
    assert not any(ctx.config.alac_library.rglob("*.m4a")), "finalize must not write a library copy"


def test_organize_after_finalize_moves_nothing(tmp_path):
    ctx = _ctx(tmp_path)
    _arrive(ctx, "a.m4a", "The Black Eyed Peas", "Hey Mama", "Hip Hop", b"one")
    FinalizeStage().execute(ctx)
    before = _paths(ctx)
    assert OrganizeStage().execute(ctx).files_changed == 0
    assert _paths(ctx) == before


def test_a_genre_correction_refiles_the_master(tmp_path):
    """Organize used to exclude the archive, so a master stayed wherever it was
    first filed. The row points at the master now; organize must follow it."""
    ctx = _ctx(tmp_path)
    _arrive(ctx, "a.m4a", "The Black Eyed Peas", "Hey Mama", "Pop", b"one")
    FinalizeStage().execute(ctx)
    ctx.conn.execute("UPDATE archive SET genre = 'Hip Hop'")
    ctx.conn.commit()
    assert OrganizeStage().execute(ctx).files_changed == 1
    (fp,) = ctx.conn.execute("SELECT file_path FROM archive").fetchone()
    assert (
        Path(fp).is_file() and Path(fp).relative_to(ctx.config.alac_archive).parts[0] == "Hip Hop"
    )
    assert Path(fp).read_bytes() == b"one"


def test_a_collision_named_pair_does_not_flip_on_every_run(tmp_path):
    ctx = _ctx(tmp_path)
    _arrive(ctx, "a.m4a", "Dion", "Don't Pity Me", "Rock", b"one")
    _arrive(ctx, "b.m4a", "Dion", "Don't Pity Me", "Rock", b"two")
    FinalizeStage().execute(ctx)
    names = sorted(Path(p).name for p in _paths(ctx).values())
    assert names == ["Dion - Don't Pity Me (2).m4a", "Dion - Don't Pity Me.m4a"]
    before = _paths(ctx)
    for run in range(3):
        assert OrganizeStage().execute(ctx).files_changed == 0, f"renamed on run {run + 1}"
        assert _paths(ctx) == before


def test_a_collision_named_library_file_does_not_flip(tmp_path):
    """The flip in the tier organize always tidied: a "(2)" file whose base
    name is taken was renamed " (3)", then back, on alternate runs."""
    from musaeus.stages.organize import library_relpath

    ctx = _ctx(tmp_path)
    lib = ctx.config.alac_library
    for i, suffix in enumerate(("", " (2)")):
        rel = library_relpath("Dion", None, "Rock", "Album", "Don't Pity Me", ".m4a")
        p = lib / rel.with_name(rel.stem + suffix + rel.suffix)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x%d" % i)
        upsert_archive(
            ctx.conn,
            {
                "file_path": str(p),
                "status": "CATALOGUED",
                "artist": "Dion",
                "album": "Album",
                "title": "Don't Pity Me",
                "genre": "Rock",
                "audio_hash": f"h{i}",
            },
        )
    ctx.conn.commit()
    before = _paths(ctx)
    for run in range(3):  # after EVERY run: two flips land back where they started
        OrganizeStage().execute(ctx)
        assert _paths(ctx) == before, f"renamed on run {run + 1}"
