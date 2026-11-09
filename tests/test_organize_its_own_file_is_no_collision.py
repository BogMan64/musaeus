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

needs_symlink = pytest.mark.skipif(
    os.name == "nt", reason="needs a symlink, which Windows allows only in Developer Mode"
)


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


def test_an_old_capitals_folder_takes_the_new_capitals(tmp_path):
    """What a case-blind disk needs: the folder is there, under old capitals."""
    from musaeus.stages.organize import _match_folder_capitals

    root = tmp_path / "ALAC-Archival"
    (root / "Rock" / "Dead or Alive" / "Album").mkdir(parents=True)
    (root / "Rock" / "Dead or Alive" / "Album" / "song.m4a").write_bytes(b"x")
    _match_folder_capitals(root / "Rock" / "Dead Or Alive" / "Album", root)
    assert os.listdir(root / "Rock") == ["Dead Or Alive"]
    assert (root / "Rock" / "Dead Or Alive" / "Album" / "song.m4a").read_bytes() == b"x"


def test_two_spellings_side_by_side_are_left_alone(tmp_path):
    from musaeus.stages.organize import _match_folder_capitals

    root = tmp_path / "ALAC-Archival"
    (root / "Rock" / "Dead or Alive").mkdir(parents=True)
    (root / "Rock" / "DEAD OR ALIVE").mkdir()
    _match_folder_capitals(root / "Rock" / "Dead Or Alive", root)
    assert sorted(os.listdir(root / "Rock")) == ["DEAD OR ALIVE", "Dead or Alive"]
