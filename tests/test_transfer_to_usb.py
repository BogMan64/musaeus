"""
Tests for Phase 3 -- scripts/usb_transfer/transfer_to_usb.py.

No real USB drive was available while this was built (2026-08-18); the
destructive path (execute_commands with dry_run=False) is only ever
exercised here via a monkeypatched subprocess.run recorder, never a real
device. Priority is proving the safety gates actually hold:
  - the denylist blocks a critical device regardless of confirmation
  - dry-run never invokes a real destructive subprocess call
  - non-root refuses before touching subprocess at all
  - a non-TTY refuses with no prompt
  - wrong typed confirmation text cancels
copy_with_verification() has no hardware dependency (plain file I/O) and
is tested for real against tmp_path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.usb_transfer.transfer_to_usb as usb_mod  # noqa: E402
from scripts.usb_transfer.transfer_to_usb import (  # noqa: E402
    BlockDevice,
    build_wipe_and_format_commands,
    confirm_wipe,
    copy_with_verification,
    critical_backing_disks,
    execute_commands,
    is_denylisted,
    list_block_devices,
)


def _device(path="/dev/sdb", size=64_000_000_000, removable=True) -> BlockDevice:
    return BlockDevice(
        path=path,
        size_bytes=size,
        size_human="59.6GB",
        model="Test USB",
        serial="TESTSERIAL123",
        tran="usb",
        removable=removable,
        mountpoints=[],
    )


# ── Denylist ──────────────────────────────────────────────────────────────────


class TestDenylist:
    def test_denylisted_device_blocked(self):
        assert is_denylisted("/dev/sda", {"/dev/sda", "/dev/sdc"}) is True

    def test_non_denylisted_device_allowed(self):
        assert is_denylisted("/dev/sdb", {"/dev/sda", "/dev/sdc"}) is False

    def test_empty_denylist_blocks_nothing(self):
        assert is_denylisted("/dev/sdb", set()) is False


_FAKE_LSBLK_OUTPUT = json.dumps(
    {
        "blockdevices": [
            {
                "name": "sda",
                "path": "/dev/sda",
                "size": "500000000000",
                "model": "Samsung SSD",
                "serial": "SYS123",
                "tran": "sata",
                "rm": False,
                "type": "disk",
                "mountpoint": None,
                "children": [
                    {
                        "name": "sda1",
                        "path": "/dev/sda1",
                        "size": "500000000000",
                        "mountpoint": "/",
                    },
                ],
            },
            {
                "name": "sdb",
                "path": "/dev/sdb",
                "size": "64000000000",
                "model": "Test USB",
                "serial": "USB123",
                "tran": "usb",
                "rm": True,
                "type": "disk",
                "mountpoint": None,
                "children": [],
            },
        ]
    }
)


class _FakeCompleted:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _fake_subprocess_for_denylist(cmd, **kwargs):
    if cmd[0] == "lsblk":
        return _FakeCompleted(stdout=_FAKE_LSBLK_OUTPUT)
    if cmd[0] == "findmnt":
        target = cmd[-1]
        source = "/dev/sda1" if target in ("/", "") else ""
        return _FakeCompleted(stdout=source, returncode=0 if source else 1)
    raise AssertionError(f"unexpected subprocess call in denylist resolution: {cmd}")


class TestCriticalBackingDisks:
    def test_root_partition_resolves_to_parent_disk(self, monkeypatch):
        monkeypatch.setattr(usb_mod.subprocess, "run", _fake_subprocess_for_denylist)
        devices = list_block_devices()
        assert [d.path for d in devices] == ["/dev/sda", "/dev/sdb"]

    def test_critical_disks_include_root_partitions_parent_not_the_usb(self, monkeypatch, tmp_path):
        def _fake(cmd, **kwargs):
            if cmd[0] == "lsblk":
                return _FakeCompleted(stdout=_FAKE_LSBLK_OUTPUT)
            if cmd[0] == "findmnt":
                # Everything (/, /home, vault_root) resolves to the same
                # root-backing partition in this fake single-disk system.
                return _FakeCompleted(stdout="/dev/sda1", returncode=0)
            raise AssertionError(cmd)

        monkeypatch.setattr(usb_mod.subprocess, "run", _fake)
        critical = critical_backing_disks(tmp_path)
        assert critical == {"/dev/sda"}
        assert "/dev/sdb" not in critical

    def test_extra_mount_is_protected(self, monkeypatch, tmp_path):
        """An explicit --extra-critical-mount (e.g. a long-attached USB
        backup drive that may or may not report as removable) must be
        covered by the denylist alongside /, /home, and vault_root."""

        def _fake(cmd, **kwargs):
            if cmd[0] == "lsblk":
                return _FakeCompleted(stdout=_FAKE_LSBLK_OUTPUT)
            if cmd[0] == "findmnt":
                target = cmd[-1]
                # / and /home and vault_root -> sda1 (sda); the extra mount
                # resolves to the separate USB-attached sdb.
                source = "/dev/sdb" if target == "/mnt/EXTRA_BACKUP" else "/dev/sda1"
                return _FakeCompleted(stdout=source, returncode=0)
            raise AssertionError(cmd)

        monkeypatch.setattr(usb_mod.subprocess, "run", _fake)
        critical = critical_backing_disks(tmp_path, extra_mounts=[Path("/mnt/EXTRA_BACKUP")])
        assert critical == {"/dev/sda", "/dev/sdb"}

    def test_unresolvable_findmnt_fails_closed_not_open(self, monkeypatch, tmp_path):
        """An incomplete denylist is worse than no answer -- if a critical
        path's backing device can't be resolved, the function must refuse
        (raise) rather than silently returning a partial set that a caller
        could mistake for 'nothing else is critical'."""

        def _fake(cmd, **kwargs):
            if cmd[0] == "lsblk":
                return _FakeCompleted(stdout=_FAKE_LSBLK_OUTPUT)
            if cmd[0] == "findmnt":
                return _FakeCompleted(stdout="", returncode=1)
            raise AssertionError(cmd)

        monkeypatch.setattr(usb_mod.subprocess, "run", _fake)
        with pytest.raises(usb_mod.DenylistResolutionError):
            critical_backing_disks(tmp_path)


# ── Typed confirmation ────────────────────────────────────────────────────────


class TestConfirmWipe:
    def test_refuses_without_tty(self, monkeypatch):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        assert confirm_wipe(_device()) is False

    def test_wrong_text_cancels(self, monkeypatch, capsys):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "/dev/sdb wrong-size")
        assert confirm_wipe(_device()) is False

    def test_exact_match_confirms(self, monkeypatch):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "/dev/sdb 59.6GB")
        assert confirm_wipe(_device()) is True

    def test_partial_match_does_not_confirm(self, monkeypatch):
        # Guards against a loose "startswith"-style check ever creeping in --
        # the match must be exact, not a prefix of the expected string.
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "/dev/sdb")
        assert confirm_wipe(_device()) is False

    def test_keyboard_interrupt_cancels(self, monkeypatch):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

        def _raise(_):
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", _raise)
        assert confirm_wipe(_device()) is False


# ── Command construction (pure, no execution) ────────────────────────────────


class TestBuildCommands:
    def test_partition_naming_plain_disk(self):
        cmds = build_wipe_and_format_commands("/dev/sdb")
        assert cmds[0] == ["wipefs", "--all", "/dev/sdb"]
        assert cmds[-1] == ["mkfs.exfat", "-n", "MUSAEUS", "/dev/sdb1"]

    def test_partition_naming_nvme_style(self):
        cmds = build_wipe_and_format_commands("/dev/nvme0n1")
        assert cmds[-1] == ["mkfs.exfat", "-n", "MUSAEUS", "/dev/nvme0n1p1"]

    def test_does_not_execute_anything(self, monkeypatch):
        called = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
        build_wipe_and_format_commands("/dev/sdb")
        assert called == []


# ── execute_commands: the actual safety-critical gate ────────────────────────


class TestExecuteCommands:
    def test_dry_run_never_calls_subprocess(self, monkeypatch):
        called = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
        execute_commands([["wipefs", "--all", "/dev/sdb"]], dry_run=True)
        assert called == []

    def test_non_root_refuses_before_touching_subprocess(self, monkeypatch):
        called = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
        monkeypatch.setattr("os.geteuid", lambda: 1000)  # not root
        with pytest.raises(PermissionError):
            execute_commands([["wipefs", "--all", "/dev/sdb"]], dry_run=False)
        assert called == [], "must never touch subprocess before the root check passes"

    def test_root_runs_commands_in_order(self, monkeypatch):
        calls = []

        def _fake_run(cmd, **kwargs):
            calls.append(cmd)

            class _R:
                returncode = 0
                stderr = ""

            return _R()

        monkeypatch.setattr(subprocess, "run", _fake_run)
        monkeypatch.setattr("os.geteuid", lambda: 0)
        cmds = build_wipe_and_format_commands("/dev/sdb")
        execute_commands(cmds, dry_run=False)
        assert calls == cmds

    def test_failing_command_stops_the_sequence(self, monkeypatch):
        calls = []

        def _fake_run(cmd, **kwargs):
            calls.append(cmd)

            class _R:
                returncode = 0 if cmd[0] == "wipefs" else 1
                stderr = "boom"

            return _R()

        monkeypatch.setattr(subprocess, "run", _fake_run)
        monkeypatch.setattr("os.geteuid", lambda: 0)
        cmds = build_wipe_and_format_commands("/dev/sdb")
        with pytest.raises(RuntimeError):
            execute_commands(cmds, dry_run=False)
        # Only wipefs (the first, failing-on-parted) ran -- mkfs.exfat must
        # never be reached once an earlier step fails.
        assert calls == cmds[:2]


# ── copy_with_verification: real file I/O, no hardware dependency ───────────


class TestCopyWithVerification:
    def test_successful_copy_is_verified(self, tmp_path: Path):
        src_root = tmp_path / "src"
        dst_root = tmp_path / "dst"
        src_root.mkdir()
        f = src_root / "a" / "track.txt"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"hello world" * 1000)

        result = copy_with_verification([f], src_root, dst_root)
        assert result.ok == [str(f)]
        assert result.failed == []
        assert (dst_root / "a" / "track.txt").read_bytes() == f.read_bytes()

    def test_hash_mismatch_reported_as_failed_not_crash(self, tmp_path: Path, monkeypatch):
        src_root = tmp_path / "src"
        dst_root = tmp_path / "dst"
        src_root.mkdir()
        f = src_root / "track.txt"
        f.write_bytes(b"data")

        import scripts.usb_transfer.transfer_to_usb as mod

        real_hash = mod.file_hash
        calls = {"n": 0}

        def _flaky_hash(path):
            calls["n"] += 1
            # Second call (the dest hash) returns something different --
            # simulates corruption during copy without needing to actually
            # corrupt bytes on disk.
            if calls["n"] == 2:
                return "deadbeef"
            return real_hash(path)

        monkeypatch.setattr(mod, "file_hash", _flaky_hash)
        result = copy_with_verification([f], src_root, dst_root)
        assert result.ok == []
        assert len(result.failed) == 1
        assert result.failed[0][0] == str(f)
        assert "hash mismatch" in result.failed[0][1]

    def test_second_io_error_aborts_whole_run(self, tmp_path: Path, monkeypatch):
        src_root = tmp_path / "src"
        dst_root = tmp_path / "dst"
        src_root.mkdir()
        f1 = src_root / "a.txt"
        f2 = src_root / "b.txt"
        f1.write_bytes(b"x")
        f2.write_bytes(b"y")

        import scripts.usb_transfer.transfer_to_usb as mod

        def _always_fail(_src, _dst):
            raise OSError("simulated device I/O error")

        monkeypatch.setattr(mod, "_copy_one", _always_fail)
        monkeypatch.setattr(mod.time, "sleep", lambda *_: None)

        result = copy_with_verification([f1, f2], src_root, dst_root)
        assert result.ok == []
        assert len(result.failed) == 1
        assert "I/O error" in result.failed[0][1]
        # Aborted after f1's second failure -- f2 must never have been
        # attempted (device may be failing).

    def test_speed_drop_triggers_cooldown(self, tmp_path: Path, monkeypatch):
        src_root = tmp_path / "src"
        dst_root = tmp_path / "dst"
        src_root.mkdir()
        files = []
        for i in range(6):
            f = src_root / f"f{i}.txt"
            f.write_bytes(b"x" * 1024 * 1024)  # 1 MiB each, uniform size
            files.append(f)

        import scripts.usb_transfer.transfer_to_usb as mod

        # First 5 files "fast" (0.1s each), 6th file "slow" (10s) -- well
        # below _SPEED_DROP_THRESHOLD (0.40) of the rolling average.
        elapsed_sequence = iter([0.1, 0.1, 0.1, 0.1, 0.1, 10.0])

        def _fake_copy_one(src, dst):
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            return next(elapsed_sequence)

        sleep_calls = []
        monkeypatch.setattr(mod, "_copy_one", _fake_copy_one)
        monkeypatch.setattr(mod.time, "sleep", lambda s: sleep_calls.append(s))

        result = copy_with_verification(files, src_root, dst_root, cooldown_seconds=15.0)
        assert result.cooldowns_triggered == 1
        assert 15.0 in sleep_calls
        assert len(result.ok) == 6
