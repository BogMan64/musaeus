"""Codex audit 2026-10-16, F1: the "safe" save followed links.

change_beside copied the song onto <name><suffix> with shutil.copy2, which
writes THROUGH a link already at that name: a planted `song.m4a.artmp` link
to another recording had that recording overwritten, and the rename then put
the link in the song's place. Cover sidecars were written with write_bytes,
through a planted `<song>.musaeus_cover.jpg` or dangling `cover.jpg` link.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from musaeus.safe_save import change_beside


def _pair(tmp_path: Path) -> tuple[Path, Path]:
    song = tmp_path / "inbox" / "song.m4a"
    other = tmp_path / "masters" / "other.m4a"
    song.parent.mkdir()
    other.parent.mkdir()
    song.write_bytes(b"SONG")
    other.write_bytes(b"ANOTHER RECORDING")
    return song, other


@pytest.mark.parametrize("how", ["symlink", "hardlink", "dangling", "stale file"])
def test_whatever_is_at_the_copys_name_is_never_written_through(tmp_path, how):
    song, other = _pair(tmp_path)
    tmp = song.with_name(song.name + ".artmp")
    if how == "symlink":
        tmp.symlink_to(other)
    elif how == "hardlink":
        os.link(other, tmp)
    elif how == "dangling":
        tmp.symlink_to(tmp_path / "masters" / "not-there.m4a")
    else:
        tmp.write_bytes(b"LEFT BY A KILLED RUN")

    change_beside(song, lambda p: p.write_bytes(p.read_bytes() + b"+TAGS"), suffix=".artmp")

    assert other.read_bytes() == b"ANOTHER RECORDING", "another recording was overwritten"
    assert not (tmp_path / "masters" / "not-there.m4a").exists()
    assert not song.is_symlink() and song.read_bytes() == b"SONG+TAGS"
    assert not tmp.exists() and not tmp.is_symlink()


def test_a_refused_change_leaves_both_files_as_they_were(tmp_path):
    song, other = _pair(tmp_path)
    tmp = song.with_name(song.name + ".tagging")
    tmp.symlink_to(other)

    def refuse(p: Path) -> None:
        raise ValueError("no")

    with pytest.raises(ValueError):
        change_beside(song, lambda p: None, suffix=".tagging", check=refuse)

    assert other.read_bytes() == b"ANOTHER RECORDING" and song.read_bytes() == b"SONG"
    assert not song.is_symlink()


@pytest.mark.parametrize("dangling", [False, True])
def test_a_cover_is_never_written_through_a_link(tmp_path, dangling):
    song, other = _pair(tmp_path)
    from musaeus.safe_save import write_new

    cover = song.parent / "cover.jpg"
    target = tmp_path / "masters" / ("nowhere.jpg" if dangling else "other.m4a")
    cover.symlink_to(target)

    write_new(cover, b"\xff\xd8\xff IMAGE")

    assert other.read_bytes() == b"ANOTHER RECORDING"
    assert not (tmp_path / "masters" / "nowhere.jpg").exists()
    assert not cover.is_symlink() and cover.read_bytes() == b"\xff\xd8\xff IMAGE"
    assert {p.name for p in song.parent.iterdir()} == {"cover.jpg", "song.m4a"}, "temp left"
