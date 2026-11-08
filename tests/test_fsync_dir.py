"""safe_save.fsync_dir: the folder fsync after a rename, which Windows cannot do.

Four copies of it raised after every successful save on Windows (Codex
survey for the Windows port, 2026-11-07). These run in both CI workflows.
"""

from __future__ import annotations

import os
import sys

import pytest

from musaeus.safe_save import change_beside, fsync_dir, fsync_file


def _folders_cannot_be_opened(monkeypatch):
    """What Windows does: os.open on a folder raises PermissionError."""
    real_open = os.open

    def open_(path, flags, *args, **kwargs):
        if os.path.isdir(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_)
    monkeypatch.setattr(sys, "platform", "win32")


def test_a_save_completes_where_folders_cannot_be_opened(tmp_path, monkeypatch):
    p = tmp_path / "a.txt"
    p.write_text("old")
    _folders_cannot_be_opened(monkeypatch)

    change_beside(p, lambda q: q.write_text("new"), suffix=".part")

    assert p.read_text() == "new"
    assert not (tmp_path / "a.txt.part").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Linux and macOS only")
def test_the_folder_is_fsynced_where_it_can_be(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd) or real(fd))

    fsync_dir(tmp_path)

    assert len(synced) == 1


def _fsync_needs_a_writable_handle(monkeypatch):
    """What Windows does: fsync on a handle opened read-only fails (EBADF)."""
    import fcntl

    real = os.fsync
    synced: list[int] = []

    def fsync(fd):
        if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY:
            raise OSError(9, "Bad file descriptor")
        synced.append(fd)
        return real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(sys, "platform", "win32")
    return synced


@pytest.mark.skipif(sys.platform == "win32", reason="simulates Windows with POSIX fcntl")
def test_a_save_completes_where_fsync_needs_a_writable_handle(tmp_path, monkeypatch):
    p = tmp_path / "a.txt"
    p.write_text("old", encoding="utf-8")
    synced = _fsync_needs_a_writable_handle(monkeypatch)

    change_beside(p, lambda q: q.write_text("new", encoding="utf-8"), suffix=".part")

    assert p.read_text(encoding="utf-8") == "new"
    assert synced, "the new copy was flushed through a writable handle"


def _windows_open_fails(monkeypatch, winerror):
    """What Windows does on a writable open it refuses: PermissionError carrying
    its own error number (5 access denied, 32 sharing violation)."""

    def refuse(path, flags, *a, **k):
        exc = PermissionError(13, "Access is denied", str(path))
        if winerror is not None:
            exc.winerror = winerror
        raise exc

    monkeypatch.setattr(os, "open", refuse)
    monkeypatch.setattr(sys, "platform", "win32")


@pytest.mark.parametrize(
    "winerror, read_only", [(5, False), (None, False), (32, True), (32, False)]
)
def test_a_refusal_that_is_not_the_read_only_flag_still_stops_the_step(
    tmp_path, monkeypatch, winerror, read_only
):
    """A sharing violation or a denying access list on a writable file must stop
    the step, or finalize could drop the source unflushed (CodeRabbit, #295)."""
    import stat

    p = tmp_path / "f.bin"
    p.write_bytes(b"x")
    if read_only:
        os.chmod(p, stat.S_IREAD)
    _windows_open_fails(monkeypatch, winerror)
    try:
        with pytest.raises(PermissionError):
            fsync_file(p)
    finally:
        os.chmod(p, stat.S_IREAD | stat.S_IWRITE)  # so the temp folder can go


def test_a_read_only_flagged_file_is_flushed_and_keeps_its_flag(tmp_path, monkeypatch):
    """copy2 carries a read-only flag; Windows then gives no writable handle and
    fsync_file raised PermissionError, stopping the step. Skipping the flush was
    not enough: finalize deletes the source next (CodeRabbit, #295). Real on
    Windows; on Linux the read-only mode refuses the writable open the same way."""
    import stat

    p = tmp_path / "ro.bin"
    p.write_bytes(b"x")
    os.chmod(p, stat.S_IREAD)
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd) or real(fd))
    monkeypatch.setattr(sys, "platform", "win32")
    try:
        fsync_file(p)
        assert len(synced) == 1, "the copy was flushed"
        assert not os.stat(p).st_mode & stat.S_IWRITE, "the flag is back"
    finally:
        os.chmod(p, stat.S_IREAD | stat.S_IWRITE)


def test_fsync_file_flushes_a_closed_file(tmp_path, monkeypatch):
    p = tmp_path / "b.bin"
    p.write_bytes(b"x")
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd) or real(fd))
    fsync_file(p)  # on Windows this is the writable-handle path for real
    assert len(synced) == 1
