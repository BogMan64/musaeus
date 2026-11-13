"""Organize must never see a song's own file as a collision.

On Windows the disk ignores capitals, so "Dead Or Alive - Song.m4a" already
"exists" while the file is still called "Dead or Alive - Song.m4a". Organize
then named the song "... (2).m4a" and left the capitals as they were. The
same happens on Linux when the archive is reached through a symlink: the row
names the real folder, the new path names the link, and both are one file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from musaeus.stages import OrganizeStage

from .test_review_of_38 import _ctx, _row

needs_symlink = pytest.mark.needs_symlinks


def _symlinked_archive(tmp_path: Path):
    real = tmp_path / "disk" / "ALAC-Archival"
    real.mkdir(parents=True)
    link = tmp_path / "Libraries" / "ALAC-Archival"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    ctx = _ctx(tmp_path, archive=link)
    song = real / "Rock" / "Dead Or Alive" / "Album" / "Dead Or Alive - Song.m4a"
    _row(ctx, song, "Dead Or Alive", "Song")
    ctx.conn.execute(
        "UPDATE archive SET canonicalized_at = datetime('now'), canon_action = 'PASSTHROUGH', "
        "finalized_at = datetime('now')"
    )
    ctx.conn.commit()
    return ctx, real


@needs_symlink
def test_a_filed_song_is_not_renamed_to_a_collision_name(tmp_path):
    ctx, real = _symlinked_archive(tmp_path)
    OrganizeStage().run(ctx)
    album = real / "Rock" / "Dead Or Alive" / "Album"
    assert sorted(p.name for p in album.iterdir()) == ["Dead Or Alive - Song.m4a"]


@needs_symlink
def test_the_preview_does_not_plan_a_collision_name_either(tmp_path):
    ctx, _real = _symlinked_archive(tmp_path)
    actions, _desc = OrganizeStage.plan_actions(ctx.conn, ctx.config)
    assert not any(" (2)" in a["destination"] for a in actions), actions


def test_an_old_capitals_folder_takes_the_new_capitals_and_its_rows_follow(tmp_path):
    """What a case-blind disk needs: the folder is there, under old capitals.
    A file this run does not refile (PENDING) still has its row follow, or a
    later scan would add it again (CodeRabbit on #308)."""
    from musaeus.stages.organize import _match_folder_capitals

    ctx = _ctx(tmp_path)
    root = ctx.config.alac_archive
    old = root / "Rock" / "Dead or Alive" / "Album" / "song.m4a"
    _row(ctx, old, "Dead Or Alive", "Song", status="PENDING")
    ctx.conn.commit()
    _match_folder_capitals(ctx.conn, root / "Rock" / "Dead Or Alive" / "Album", root)
    new = root / "Rock" / "Dead Or Alive" / "Album" / "song.m4a"
    assert os.listdir(root / "Rock") == ["Dead Or Alive"]
    assert new.read_bytes() == b"x"
    assert ctx.conn.execute("SELECT file_path FROM archive").fetchone()[0] == str(new)


def test_a_database_error_puts_the_folder_back(tmp_path, monkeypatch):
    from musaeus.stages import organize

    ctx = _ctx(tmp_path)
    root = ctx.config.alac_archive
    old = root / "Rock" / "Dead or Alive" / "Album" / "song.m4a"
    _row(ctx, old, "Dead Or Alive", "Song", status="PENDING")
    ctx.conn.commit()
    monkeypatch.setattr(organize, "_PATH_COLUMNS", (("no_such_column_table", "x"),))
    ctx.conn.execute("CREATE TABLE no_such_column_table (y TEXT)")
    organize._match_folder_capitals(ctx.conn, root / "Rock" / "Dead Or Alive" / "Album", root)
    assert os.listdir(root / "Rock") == ["Dead or Alive"]
    assert ctx.conn.execute("SELECT file_path FROM archive").fetchone()[0] == str(old)


@pytest.mark.skipif(os.name == "nt", reason="a case-blind disk cannot hold two spellings")
def test_two_spellings_side_by_side_are_left_alone(tmp_path):
    from musaeus.stages.organize import _match_folder_capitals

    ctx = _ctx(tmp_path)
    root = ctx.config.alac_archive
    (root / "Rock" / "Dead or Alive").mkdir(parents=True)
    (root / "Rock" / "DEAD OR ALIVE").mkdir()
    _match_folder_capitals(ctx.conn, root / "Rock" / "Dead Or Alive", root)
    assert sorted(os.listdir(root / "Rock")) == ["DEAD OR ALIVE", "Dead or Alive"]


def test_an_album_capitals_change_reaches_the_folder(tmp_path):
    """On Windows the file name stays and the parents compare equal, so
    neither branch ran (CodeRabbit on #308). Runs on every disk."""
    ctx = _ctx(tmp_path)
    artist = ctx.config.alac_archive / "Rock" / "Dead Or Alive"
    _row(ctx, artist / "album" / "Dead Or Alive - Song.m4a", "Dead Or Alive", "Song")
    ctx.conn.execute(
        "UPDATE archive SET canonicalized_at = datetime('now'), canon_action = 'PASSTHROUGH', "
        "finalized_at = datetime('now')"
    )
    ctx.conn.commit()
    OrganizeStage().run(ctx)
    assert os.listdir(artist) == ["Album"]
    assert os.listdir(artist / "Album") == ["Dead Or Alive - Song.m4a"]
    (fp,) = ctx.conn.execute("SELECT file_path FROM archive").fetchone()
    assert fp == str(artist / "Album" / "Dead Or Alive - Song.m4a")
