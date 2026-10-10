"""The car stick's filesystem is checked (and repaired) before a copy, and the
stick is ejected after (Grey, 2026-10-10: the Android unit called the stick
corrupted after a sync, and Windows had to repair it).

udisks is faked here: the calls and their order are what is tested. A real
run needs the stick plugged in.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.usb_transfer.transfer_to_usb as usb_mod  # noqa: E402
from scripts.usb_transfer.transfer_to_usb import (  # noqa: E402
    UsbTargetError,
    check_and_repair_filesystem,
    safe_eject,
)


class FakeUdisks:
    """findmnt, udisksctl and gdbus, as udisks answers them."""

    def __init__(self, consistent=True, repaired=True, mount_at="/media/grey/STICK"):
        self.calls: list[list[str]] = []
        self.consistent, self.repaired, self.mount_at = consistent, repaired, mount_at

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)

        def ok(out=""):
            return subprocess.CompletedProcess(cmd, 0, out, "")

        if cmd[0] == "findmnt":
            return ok("/dev/sde1\n")
        if cmd[:2] == ["udisksctl", "mount"]:
            return ok(f"Mounted /dev/sde1 at {self.mount_at}\n")
        if cmd[0] == "udisksctl":
            return ok()
        if cmd[-2].endswith(".Check"):
            return ok("(true,)\n" if self.consistent else "(false,)\n")
        if cmd[-2].endswith(".Repair"):
            return ok("(true,)\n" if self.repaired else "(false,)\n")
        raise AssertionError(cmd)

    def names(self) -> list[str]:
        return [
            c[-2].rsplit(".", 1)[-1] if c[0] == "gdbus" else " ".join(c[:2]) for c in self.calls
        ]


@pytest.fixture(autouse=True)
def mounted(monkeypatch):
    monkeypatch.setattr(usb_mod, "_findmnt_target", lambda part: "/media/grey/STICK")
    monkeypatch.setattr(usb_mod, "_backing_disk_for_path", lambda p: "/dev/sde")


def test_a_clean_stick_is_checked_and_mounted_again_where_it_was(tmp_path):
    fake = FakeUdisks()
    root, said = check_and_repair_filesystem(Path("/media/grey/STICK/Music"), run=fake)
    assert root == Path("/media/grey/STICK/Music")
    assert "no errors" in said
    assert fake.names() == ["findmnt -no", "udisksctl unmount", "Check", "udisksctl mount"]


def test_a_stick_with_errors_is_repaired(tmp_path):
    fake = FakeUdisks(consistent=False)
    root, said = check_and_repair_filesystem(Path("/media/grey/STICK/Music"), run=fake)
    assert "repaired" in said
    assert fake.names()[2:] == ["Check", "Repair", "udisksctl mount"]


def test_a_failed_repair_refuses_the_copy_and_mounts_the_stick_again(tmp_path):
    fake = FakeUdisks(consistent=False, repaired=False)
    with pytest.raises(UsbTargetError, match="could not be repaired"):
        check_and_repair_filesystem(Path("/media/grey/STICK/Music"), run=fake)
    assert fake.names()[-1] == "udisksctl mount"


def test_a_stick_mounted_somewhere_new_is_followed(tmp_path):
    fake = FakeUdisks(mount_at="/media/grey/STICK1")
    root, _ = check_and_repair_filesystem(Path("/media/grey/STICK/Music"), run=fake)
    assert root == Path("/media/grey/STICK1/Music")


def test_ejecting_flushes_unmounts_and_powers_off(monkeypatch):
    synced: list[bool] = []
    monkeypatch.setattr(usb_mod.os, "sync", lambda: synced.append(True))
    fake = FakeUdisks()
    said = safe_eject(Path("/media/grey/STICK/Music"), run=fake)
    assert synced and "safe to remove" in said
    assert fake.calls[-2:] == [["udisksctl", "unmount", "-b", "/dev/sde1"],
                               ["udisksctl", "power-off", "-b", "/dev/sde"]]  # fmt: skip
