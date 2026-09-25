"""A run ends with masters in ALAC-Archival and -18 LUFS copies in ALAC_Library.

Grey decided on 2026-08-18 that new work lands in the pristine masters tier
and every edition is baked from it; Finalize never switched, so the first
batch on the fresh vault (2026-09-24) ended with no masters tree at all and a
doctor warning. These tests hold Act 3's filing steps to the convention every
edition tool relies on -- archive.file_path is the ALAC_Library copy, its
master sits at the same relative path under ALAC-Archival -- and to the two
ways that convention used to break: organize moving a copy without its master,
and organize's " (N)" names flipping on every run.

Real audio (ffmpeg's sine source), because the bake measures and re-encodes it.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.editions import master_path_for
from musaeus.stages.finalize import FinalizeStage
from musaeus.stages.library_bake import LibraryBakeStage
from musaeus.stages.organize import OrganizeStage

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _tone(path: Path, freq: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration=4",
            "-c:a",
            "alac",
            str(path),
        ],
        check=True,
    )


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
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


def _arrive(ctx: RunContext, name: str, artist: str, title: str, genre: str, freq: int) -> None:
    p = ctx.inbox / name
    _tone(p, freq)
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(p),
            "status": "CATALOGUED",
            "artist": artist,
            "album": "Album",
            "title": title,
            "genre": genre,
            "audio_hash": f"pcm{freq}",
        },
    )
    ctx.conn.execute(
        "UPDATE archive SET canonicalized_at = datetime('now'), canon_action = 'PASSTHROUGH' WHERE file_path = ?",
        (str(p),),
    )
    ctx.conn.commit()


def _act3_filing(ctx: RunContext) -> dict[str, str]:
    """Finalize -> LibraryBake -> Organize, the Act 3 steps that place files.
    Returns each master's checksum as Finalize left it."""
    assert FinalizeStage().execute(ctx).success
    masters = {r[0]: _sha(Path(r[0])) for r in ctx.conn.execute("SELECT file_path FROM archive")}
    bake = LibraryBakeStage().execute(ctx)
    assert bake.files_errored == 0, bake.errors
    assert OrganizeStage().execute(ctx).success
    return masters


def _paths(ctx):
    return {r[0]: r[1] for r in ctx.conn.execute("SELECT id, file_path FROM archive")}


def _assert_every_copy_has_its_master(ctx):
    lib = ctx.config.alac_library
    for (fp,) in ctx.conn.execute("SELECT file_path FROM archive"):
        fp = Path(fp)
        assert fp.is_relative_to(lib), f"row not pointing at its library copy: {fp}"
        m = master_path_for(fp, lib, ctx.config.alac_archive)
        assert m.is_master and m.path.is_file(), f"no master for {fp}"


def test_a_run_ends_with_masters_and_baked_library_copies(ctx):
    _arrive(ctx, "a.m4a", "The Black Eyed Peas", "Hey Mama", "Hip Hop", 440)
    _arrive(ctx, "b.m4a", "Paul McCartney & Stevie Wonder", "Ebony and Ivory", "Rock", 550)
    masters_as_filed = _act3_filing(ctx)

    _assert_every_copy_has_its_master(ctx)
    for master, sha in masters_as_filed.items():
        assert Path(master).is_file() and _sha(Path(master)) == sha, (
            "a master was altered after Finalize"
        )
    baked = ctx.conn.execute(
        "SELECT COUNT(*) FROM archive WHERE lufs_baked_at IS NOT NULL"
    ).fetchone()[0]
    assert baked == 2
    assert (ctx.config.alac_archive / "Hip Hop" / "Black Eyed Peas, The").is_dir()


def test_the_next_organize_moves_nothing(ctx):
    _arrive(ctx, "a.m4a", "The Black Eyed Peas", "Hey Mama", "Hip Hop", 440)
    _act3_filing(ctx)
    before = _paths(ctx)
    assert OrganizeStage().execute(ctx).files_changed == 0
    assert _paths(ctx) == before


def test_a_genre_correction_moves_the_copy_and_its_master_together(ctx):
    """The old library's 390 master-less rows came from exactly this: organize
    refiled a library copy and left its master where it was."""
    _arrive(ctx, "a.m4a", "The Black Eyed Peas", "Hey Mama", "Pop", 440)
    _act3_filing(ctx)
    ctx.conn.execute("UPDATE archive SET genre = 'Hip Hop'")
    ctx.conn.commit()
    assert OrganizeStage().execute(ctx).files_changed == 1
    (fp,) = ctx.conn.execute("SELECT file_path FROM archive").fetchone()
    assert "/Hip Hop/" in fp
    _assert_every_copy_has_its_master(ctx)
    assert not any((ctx.config.alac_archive / "Pop").rglob("*.m4a")), (
        "master left behind in the old genre"
    )


def test_a_collision_named_pair_does_not_flip_on_every_run(ctx):
    """Two different recordings, one name: the second is " (2)". Organize used
    to ask unique_path again, get " (3)", then " (2)" the run after -- each
    flip freeing a path and breaking the master mirror."""
    _arrive(ctx, "a.m4a", "Dion", "Don't Pity Me", "Rock", 440)
    _arrive(ctx, "b.m4a", "Dion", "Don't Pity Me", "Rock", 660)
    _act3_filing(ctx)
    names = sorted(Path(r[0]).name for r in ctx.conn.execute("SELECT file_path FROM archive"))
    assert names == ["Dion - Don't Pity Me (2).m4a", "Dion - Don't Pity Me.m4a"]
    before = _paths(ctx)
    for _ in range(2):
        assert OrganizeStage().execute(ctx).files_changed == 0
    assert _paths(ctx) == before
    _assert_every_copy_has_its_master(ctx)
