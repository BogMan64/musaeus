"""Reading the immutable flag without root, and never guessing "locked".

The archive's protection is chattr +i. The tag writers need to know it BEFORE
they copy a file, so the check must be cheap and must not use lsattr.

The mocked tests say `sys.platform` is linux themselves: `is_immutable`
answers False on a non-Linux host before it reads a flag, so without that
they would fail on the Windows baseline job for a reason that is not a bug.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from musaeus import immutability as imm


@pytest.fixture
def as_linux(monkeypatch):
    """Read the flag on any host: the check itself is platform-independent."""
    monkeypatch.setattr(imm.sys, "platform", "linux")


def test_the_immutable_bit_is_reported(tmp_path, monkeypatch, as_linux):
    p = tmp_path / "a.m4a"
    p.write_bytes(b"x")
    monkeypatch.setattr(imm, "_get_flags", lambda fd: imm.FS_IMMUTABLE_FL)
    assert imm.is_immutable(p) is True


def test_a_clear_flag_is_not_locked(tmp_path, monkeypatch, as_linux):
    p = tmp_path / "a.m4a"
    p.write_bytes(b"x")
    monkeypatch.setattr(imm, "_get_flags", lambda fd: 0)
    assert imm.is_immutable(p) is False


def test_a_filesystem_without_the_ioctl_is_not_called_locked(tmp_path, monkeypatch, as_linux):
    """Doubt means "not locked": a wrong "locked" would stop a working run."""
    p = tmp_path / "a.m4a"
    p.write_bytes(b"x")

    def unsupported(fd: int) -> int:
        raise OSError(25, "Inappropriate ioctl for device")

    monkeypatch.setattr(imm, "_get_flags", unsupported)
    assert imm.is_immutable(p) is False


def test_a_non_linux_host_is_not_called_locked(tmp_path, monkeypatch):
    p = tmp_path / "a.m4a"
    p.write_bytes(b"x")
    monkeypatch.setattr(imm.sys, "platform", "win32")
    monkeypatch.setattr(imm, "_get_flags", lambda fd: imm.FS_IMMUTABLE_FL)
    assert imm.is_immutable(p) is False


def test_a_missing_file_is_not_called_locked(tmp_path):
    assert imm.is_immutable(tmp_path / "gone.m4a") is False


@pytest.mark.skipif(sys.platform == "win32", reason="a FIFO is a POSIX thing")
def test_a_fifo_named_like_a_song_does_not_block_the_scan(tmp_path):
    """A FIFO passes the suffix filter; a blocking open would wait for ever."""
    fifo = tmp_path / "song.m4a"
    os.mkfifo(fifo)
    code = f"from musaeus.immutability import is_immutable;print(is_immutable({str(fifo)!r}))"
    # A timeout, not a bare call: the regression this guards is a hang.
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.stdout.strip() == "False", done.stderr


def test_locked_in_counts_only_music_files(tmp_path, monkeypatch):
    (tmp_path / "Artist" / "Album").mkdir(parents=True)
    song = tmp_path / "Artist" / "Album" / "Artist - Song.m4a"
    song.write_bytes(b"x")
    (tmp_path / "cover.jpg").write_bytes(b"x")
    monkeypatch.setattr(imm, "is_immutable", lambda p: Path(p).suffix == ".m4a")

    count, examples = imm.locked_in(tmp_path)
    assert count == 1
    assert examples == [song]


def test_the_unlock_hint_quotes_a_path_with_spaces():
    """Copied into a shell, an unquoted "Album Name" is two arguments."""
    hint = imm.unlock_hint("/vault/Artist/Album Name")
    assert hint == "sudo chattr -R -i '/vault/Artist/Album Name'"


def test_the_lock_hint_quotes_too():
    assert imm.lock_hint("/vault/Artist/Album Name") == (
        "sudo chattr -R +i '/vault/Artist/Album Name'"
    )


def _chattr_works(where: Path) -> bool:
    """Whether this host can set the immutable flag at all.

    Root is not enough: it needs CAP_LINUX_IMMUTABLE, and the filesystem must
    carry the flag. A wrong skip here just loses one optional test.
    """
    if not sys.platform.startswith("linux"):
        return False
    probe = where / "probe.m4a"
    probe.write_bytes(b"x")
    try:
        r = subprocess.run(
            ["chattr", "+i", str(probe)], capture_output=True, text=True, check=False
        )
    except OSError:
        return False
    if r.returncode != 0:
        return False
    subprocess.run(["chattr", "-i", str(probe)], capture_output=True, check=False)
    return True


def test_a_real_locked_file_is_detected(tmp_path):
    if not _chattr_works(tmp_path):
        pytest.skip("chattr +i is not available here (needs root and CAP_LINUX_IMMUTABLE)")
    p = tmp_path / "a.m4a"
    p.write_bytes(b"x")
    subprocess.run(["chattr", "+i", str(p)], check=True)
    try:
        assert imm.is_immutable(p) is True
        assert imm.locked_in(tmp_path)[0] == 1
    finally:
        subprocess.run(["chattr", "-i", str(p)], check=False)
