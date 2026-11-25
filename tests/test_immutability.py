"""Reading the immutable flag without root, and never guessing "locked".

The archive's protection is chattr +i. The tag writers need to know it BEFORE
they copy a file, so the check must be cheap and must not use lsattr.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from musaeus import immutability as imm


def test_the_immutable_bit_is_reported(tmp_path, monkeypatch):
    p = tmp_path / "a.m4a"
    p.write_bytes(b"x")
    monkeypatch.setattr(imm, "_get_flags", lambda fd: imm.FS_IMMUTABLE_FL)
    assert imm.is_immutable(p) is True


def test_a_clear_flag_is_not_locked(tmp_path, monkeypatch):
    p = tmp_path / "a.m4a"
    p.write_bytes(b"x")
    monkeypatch.setattr(imm, "_get_flags", lambda fd: 0)
    assert imm.is_immutable(p) is False


def test_a_filesystem_without_the_ioctl_is_not_called_locked(tmp_path, monkeypatch):
    """Doubt means "not locked": a wrong "locked" would stop a working run."""
    p = tmp_path / "a.m4a"
    p.write_bytes(b"x")

    def unsupported(fd: int) -> int:
        raise OSError(25, "Inappropriate ioctl for device")

    monkeypatch.setattr(imm, "_get_flags", unsupported)
    assert imm.is_immutable(p) is False


def test_a_missing_file_is_not_called_locked(tmp_path):
    assert imm.is_immutable(tmp_path / "gone.m4a") is False


def test_locked_in_counts_only_music_files(tmp_path, monkeypatch):
    (tmp_path / "Artist" / "Album").mkdir(parents=True)
    song = tmp_path / "Artist" / "Album" / "Artist - Song.m4a"
    song.write_bytes(b"x")
    (tmp_path / "cover.jpg").write_bytes(b"x")
    monkeypatch.setattr(imm, "is_immutable", lambda p: Path(p).suffix == ".m4a")

    count, examples = imm.locked_in(tmp_path)
    assert count == 1
    assert examples == [song]


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or os.geteuid() != 0,
    reason="chattr +i needs root",
)
def test_a_real_locked_file_is_detected(tmp_path):
    p = tmp_path / "a.m4a"
    p.write_bytes(b"x")
    subprocess.run(["chattr", "+i", str(p)], check=True)
    try:
        assert imm.is_immutable(p) is True
        assert imm.locked_in(tmp_path)[0] == 1
    finally:
        subprocess.run(["chattr", "-i", str(p)], check=False)
