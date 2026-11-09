"""`musaeus iphone-sync` (Grey, 2026-11-09): the iPhone steps, run by MUSAEUS.

The phone tools are replaced here; a plain folder stands in for VLC's
Documents folder. What matters: the size comes from the phone's real free
space, VLC is cleared only on a typed "yes", and nothing is cleared or copied
unless the folder really is the phone's mount.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest

from musaeus import iphone_sync as s

FREE = 25_000_000_000


@pytest.fixture
def phone(tmp_path, monkeypatch):
    src = tmp_path / "iPHONE_Library"
    (src / "Toto" / "IV").mkdir(parents=True)
    (src / "Toto" / "IV" / "Toto - Rosanna.m4a").write_bytes(b"a" * 1000)
    (src / "Toto" / "IV" / "half.m4a.edition_tmp").write_bytes(b"x")
    mount = tmp_path / "iphone"
    mount.mkdir()
    (mount / "Old - Song.m4a").write_bytes(b"o" * 500)
    st = {"mounted": False, "unmounts": 0, "builds": [], "rsyncs": 0, "free": FREE}

    def mount_(m):
        st["mounted"] = True
        return True

    def unmount(m):
        st["mounted"] = False
        st["unmounts"] += 1

    def rsync(a, b):
        assert st["mounted"], "copied while the phone was not mounted"
        st["rsyncs"] += 1
        shutil.copytree(a, b, dirs_exist_ok=True, ignore=shutil.ignore_patterns("*.edition_tmp"))
        return 0

    monkeypatch.setattr(s.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(s, "_attached", lambda: True)
    monkeypatch.setattr(s, "_paired", lambda: True)
    monkeypatch.setattr(s, "_free_bytes", lambda: st["free"])
    monkeypatch.setattr(s, "_mount", mount_)
    monkeypatch.setattr(s, "_unmount", unmount)
    monkeypatch.setattr(s, "_build", lambda gb: st["builds"].append(gb) or 0)
    monkeypatch.setattr(s, "_rsync", rsync)
    return src, mount, st


def test_yes_sizes_the_edition_from_the_phone_then_clears_and_copies(phone):
    src, mount, st = phone
    assert s.sync(src, mount, ask=lambda q: "yes") == 0
    assert st["builds"] == [math.floor((FREE + 500 - s.HEADROOM) / 1e7) / 100]
    assert not (mount / "Old - Song.m4a").exists(), "VLC was not cleared"
    assert (mount / "Toto" / "IV" / "Toto - Rosanna.m4a").is_file()
    assert not st["mounted"] and st["unmounts"] == 2


def test_anything_but_yes_deletes_nothing_and_does_not_rebuild(phone):
    src, mount, st = phone
    assert s.sync(src, mount, ask=lambda q: "y") == 0
    assert (mount / "Old - Song.m4a").is_file(), "deleted without a typed yes"
    assert st["builds"] == []
    assert (mount / "Toto" / "IV" / "Toto - Rosanna.m4a").is_file()


def test_without_yes_a_copy_that_does_not_fit_is_not_started(phone):
    src, mount, st = phone
    st["free"] = s.HEADROOM + 999  # the 1000-byte song does not fit
    assert s.sync(src, mount, ask=lambda q: "") == 1
    assert st["rsyncs"] == 0 and (mount / "Old - Song.m4a").is_file()
    assert not st["mounted"]


def test_a_failed_build_leaves_the_phone_alone(phone, monkeypatch):
    src, mount, st = phone
    monkeypatch.setattr(s, "_build", lambda gb: 2)
    assert s.sync(src, mount, ask=lambda q: "yes") == 2
    assert (mount / "Old - Song.m4a").is_file() and st["rsyncs"] == 0


def test_too_little_room_is_refused_before_any_build(phone):
    src, mount, st = phone
    st["free"] = s.HEADROOM - 10_000
    assert s.sync(src, mount, ask=lambda q: "yes") == 1
    assert st["builds"] == [] and (mount / "Old - Song.m4a").is_file()


def test_a_folder_that_is_not_the_phone_is_never_cleared(phone, monkeypatch):
    """ifuse reported success but nothing is mounted: the folder is the PC's."""
    src, mount, st = phone
    monkeypatch.undo()
    calls = []
    monkeypatch.setattr(s.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(s, "_attached", lambda: True)
    monkeypatch.setattr(s, "_paired", lambda: True)
    monkeypatch.setattr(s, "_free_bytes", lambda: FREE)
    monkeypatch.setattr(s, "_build", lambda gb: calls.append("build") or 0)
    monkeypatch.setattr(s, "_rsync", lambda a, b: calls.append("rsync") or 0)

    class _Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(s, "_run", lambda cmd, timeout=60: calls.append(cmd[0]) or _Done())
    empty = mount.parent / "empty_mount"
    assert s.sync(src, empty, ask=lambda q: "yes") == 1
    assert "build" not in calls and "rsync" not in calls
    # A plain folder with files in it is refused before ifuse is even run.
    calls.clear()
    assert s._mount(mount) is False
    assert "ifuse" not in calls and (mount / "Old - Song.m4a").is_file()


def test_the_command_is_wired_into_the_cli():
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "-m", "musaeus", "iphone-sync", "--help"],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1], timeout=60,
    )  # fmt: skip
    assert r.returncode == 0 and "--mount" in r.stdout


def test_an_edition_too_big_for_the_phone_is_not_started_and_vlc_is_kept(phone):
    """CodeRabbit on #310: a build can leave the edition over its budget. VLC
    must not be cleared for a copy that cannot fit."""
    src, mount, st = phone
    st["free"] = s.HEADROOM + 1000 - 500  # after clearing: room for 1000 bytes, the edition is 1001
    (src / "Toto" / "IV" / "extra.m4a").write_bytes(b"e")
    assert s.sync(src, mount, ask=lambda q: "yes") == 1
    assert (mount / "Old - Song.m4a").is_file(), "VLC cleared for a copy that cannot fit"
    assert st["rsyncs"] == 0 and not st["mounted"]
