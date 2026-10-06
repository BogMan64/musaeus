"""--sync brings a stick in line with the car library (Grey, 2026-10-05).

The stick was a snapshot: songs deleted, renamed or moved to review since the copy stayed on
it. A sync copies what is new or changed, deletes what the library no longer has, leaves the
rest, and never touches a file type the library does not hold or a hidden/system folder.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.usb_transfer.transfer_to_usb as usb_mod  # noqa: E402
from scripts.usb_transfer.transfer_to_usb import (  # noqa: E402
    SyncPlan,
    UsbTargetError,
    apply_sync_deletions,
    check_sync_space,
    check_sync_target,
    plan_sync,
)


def _w(root: Path, rel: str, data: bytes = b"x" * 10) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


@pytest.fixture
def trees(tmp_path):
    src, dst = tmp_path / "CAR_Library", tmp_path / "stick"
    src.mkdir()
    dst.mkdir()
    return src, dst


def _files(src):
    return sorted(p for p in src.rglob("*") if p.is_file())


def test_new_changed_unchanged_and_gone(trees):
    src, dst = trees
    _w(src, "A/Al/A - Same.m4a")
    _w(dst, "A/Al/A - Same.m4a")
    _w(src, "A/Al/A - New.m4a")
    _w(src, "A/Al/A - Retagged.m4a", b"y" * 12)
    _w(dst, "A/Al/A - Retagged.m4a", b"y" * 10)
    _w(dst, "B/Bl/B - Deleted.m4a")
    plan = plan_sync(_files(src), src, dst)
    assert (plan.new, plan.changed, plan.unchanged, plan.case_only) == (1, 1, 1, 0)
    assert [p.name for p in plan.delete] == ["B - Deleted.m4a"]
    assert sorted(p.name for p in plan.copy) == ["A - New.m4a", "A - Retagged.m4a"]
    assert plan.replaced_bytes == 10


def test_a_letter_case_rename_is_deleted_and_copied_again(trees):
    # USHER/ became Usher/ (2026-10-04); FAT32 cannot rename by case alone
    src, dst = trees
    _w(src, "Usher/Confessions/Usher - My Boo.m4a")
    _w(dst, "USHER/Confessions/Usher - My Boo.m4a")
    plan = plan_sync(_files(src), src, dst)
    assert plan.case_only == 1
    assert [p.relative_to(dst).as_posix() for p in plan.delete] == [
        "USHER/Confessions/Usher - My Boo.m4a"
    ]
    assert [p.relative_to(src).as_posix() for p in plan.copy] == [
        "Usher/Confessions/Usher - My Boo.m4a"
    ]
    apply_sync_deletions(plan, dst)
    assert not (dst / "USHER").exists()  # the emptied folders go too


def test_files_the_library_does_not_hold_and_hidden_folders_are_never_deleted(trees):
    src, dst = trees
    _w(src, "A/Al/A - Song.m4a")
    keep = [
        _w(dst, "notes.txt"),
        _w(dst, "System Volume Information/IndexerVolumeGuid.m4a"),
        _w(dst, ".Trashes/old.m4a"),
        _w(dst, "Android/data/x.db"),
    ]
    plan = plan_sync(_files(src), src, dst)
    assert plan.delete == []
    apply_sync_deletions(plan, dst)
    assert all(p.exists() for p in keep)


def test_the_rewritten_playlists_are_not_deleted(trees):
    src, dst = trees
    _w(src, "Playlists/All.m3u8")
    _w(dst, "Playlists/Favourites.m3u8")  # written by copy_playlists, not in the library
    plan = plan_sync(_files(src), src, dst, keep_names={"Playlists/Favourites.m3u8"})
    assert plan.delete == []


def test_deletions_leave_non_empty_folders_and_the_target_root(trees):
    src, dst = trees
    _w(src, "A/Al/A - Stays.m4a")
    _w(dst, "A/Al/A - Stays.m4a")
    _w(dst, "A/Al/A - Gone.m4a")
    plan = plan_sync(_files(src), src, dst)
    assert apply_sync_deletions(plan, dst) == 1
    assert (dst / "A/Al/A - Stays.m4a").exists()
    assert dst.exists()


def test_space_counts_what_the_deletions_free(trees, monkeypatch):
    src, dst = trees
    new = _w(src, "A/A - New.m4a", b"n" * 100)
    gone = _w(dst, "B/B - Gone.m4a", b"g" * 80)
    plan = SyncPlan(copy=[new], delete=[gone])
    monkeypatch.setattr(usb_mod.shutil, "disk_usage", lambda p: type("U", (), {"free": 30})())
    check_sync_space(dst, plan)  # 100 needed <= 30 free + 80 freed
    monkeypatch.setattr(usb_mod.shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())
    with pytest.raises(UsbTargetError, match="not enough free space"):
        check_sync_space(dst, plan)


@pytest.fixture
def removable_stick(monkeypatch):
    monkeypatch.setattr(usb_mod, "_backing_disk_for_path", lambda p: "/dev/sde")
    monkeypatch.setattr(
        usb_mod, "critical_backing_disks", lambda v, e: {"/dev/nvme0n1", "/dev/sda"}
    )
    stick = type("D", (), {"path": "/dev/sde"})()
    monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [stick])


def test_a_removable_stick_with_a_few_changes_is_accepted(tmp_path, removable_stick):
    check_sync_target(
        tmp_path, SyncPlan(delete=[Path("x")] * 5, target_library_files=100), tmp_path, []
    )


def test_deleting_more_than_a_quarter_of_the_stick_is_refused(tmp_path, removable_stick):
    plan = SyncPlan(delete=[Path("x")] * 30, target_library_files=100)
    with pytest.raises(UsbTargetError, match="Wrong stick or wrong library"):
        check_sync_target(tmp_path, plan, tmp_path, [])


def test_letter_case_renames_do_not_count_as_deletions(tmp_path, removable_stick):
    plan = SyncPlan(delete=[Path("x")] * 30, case_only=30, target_library_files=100)
    check_sync_target(tmp_path, plan, tmp_path, [])


def test_a_critical_disk_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(usb_mod, "_backing_disk_for_path", lambda p: "/dev/sda")
    monkeypatch.setattr(usb_mod, "critical_backing_disks", lambda v, e: {"/dev/sda"})
    monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [])
    with pytest.raises(UsbTargetError, match="backs /, /home or the vault"):
        check_sync_target(tmp_path, SyncPlan(), tmp_path, [])


def test_a_disk_that_is_not_removable_is_refused(tmp_path, monkeypatch):
    # USB1 / USB2 / NUC8TB are data drives, not sticks
    monkeypatch.setattr(usb_mod, "_backing_disk_for_path", lambda p: "/dev/sdc")
    monkeypatch.setattr(usb_mod, "critical_backing_disks", lambda v, e: {"/dev/sda"})
    monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [])
    with pytest.raises(UsbTargetError, match="not a removable device"):
        check_sync_target(tmp_path, SyncPlan(), tmp_path, [])


def test_an_unknown_device_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(usb_mod, "_backing_disk_for_path", lambda p: None)
    with pytest.raises(UsbTargetError, match="could not tell"):
        check_sync_target(tmp_path, SyncPlan(), tmp_path, [])


def test_sync_needs_no_format(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["transfer_to_usb.py", "--library", "car", "--sync"])
    with pytest.raises(SystemExit):
        usb_mod.main()
    assert "--sync works on the filesystem already there" in capsys.readouterr().err


def test_a_dry_run_changes_nothing(trees, monkeypatch, removable_stick, tmp_path):
    src, dst = trees
    _w(src, "A/A - New.m4a")
    gone = _w(dst, "B/B - Gone.m4a")
    _w(dst, "C/C - Keep1.m4a")
    _w(src, "C/C - Keep1.m4a")
    for i in range(6):
        _w(src, f"D/D{i}.m4a")
        _w(dst, f"D/D{i}.m4a")
    cfg = type("C", (), {"vault_root": tmp_path})()
    args = type("A", (), {"execute": False, "cooldown_seconds": 0})()
    assert usb_mod._run_sync(args, cfg, _files(src), src, dst, []) == 0
    assert gone.exists() and not (dst / "A/A - New.m4a").exists()


def test_execute_deletes_then_copies_verified(trees, monkeypatch, removable_stick, tmp_path):
    src, dst = trees
    _w(src, "A/A - New.m4a", b"new")
    gone = _w(dst, "B/B - Gone.m4a")
    for i in range(6):
        _w(src, f"D/D{i}.m4a")
        _w(dst, f"D/D{i}.m4a")
    cfg = type("C", (), {"vault_root": tmp_path})()
    args = type("A", (), {"execute": True, "cooldown_seconds": 0})()
    assert usb_mod._run_sync(args, cfg, _files(src), src, dst, []) == 0
    assert not gone.exists() and not (dst / "B").exists()
    assert (dst / "A/A - New.m4a").read_bytes() == b"new"
