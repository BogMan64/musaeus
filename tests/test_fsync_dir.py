"""safe_save.fsync_dir: the folder fsync after a rename, which Windows cannot do.

Four copies of it raised after every successful save on Windows (Codex
survey for the Windows port, 2026-11-07). These run in both CI workflows.
"""

from __future__ import annotations

import os
import sys

import pytest

from musaeus.safe_save import change_beside, fsync_dir


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
