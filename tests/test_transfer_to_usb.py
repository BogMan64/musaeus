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
from musaeus.config import MusicConfig  # noqa: E402
from scripts.usb_transfer.transfer_to_usb import (  # noqa: E402
    BlockDevice,
    CopyResult,
    FormatToolingError,
    UsbTargetError,
    build_unmount_commands,
    build_wipe_and_format_commands,
    check_free_space,
    check_mkfs_available,
    confirm_wipe,
    copy_playlists,
    copy_with_verification,
    critical_backing_disks,
    execute_commands,
    files_too_big_for_fat32,
    is_denylisted,
    list_block_devices,
    mounted_dir_for_device,
    partition_path_for,
    validate_label,
    wait_for_partition,
)

# Any of these appearing in a --no-format run's subprocess calls is the bug
# that path exists to make impossible.
_DESTRUCTIVE = {"umount", "wipefs", "parted", "mkfs.exfat", "mkfs.vfat", "mount"}


def _device(
    path="/dev/sdb", size=64_000_000_000, removable=True, mountpoints=None
) -> BlockDevice:
    return BlockDevice(
        path=path,
        size_bytes=size,
        size_human="59.6GB",
        model="Test USB",
        serial="TESTSERIAL123",
        tran="usb",
        removable=removable,
        mountpoints=mountpoints if mountpoints is not None else [],
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


class TestBuildUnmountCommands:
    """2026-09-03: a real run hit wipefs failing with "Device or resource
    busy" because the target was still auto-mounted (e.g. at
    /media/grey/MyTunes from being plugged in) and nothing unmounted it
    first."""

    def test_no_mountpoints_means_no_commands(self):
        dev = _device(mountpoints=[])
        assert build_unmount_commands(dev) == []

    def test_one_mountpoint_becomes_one_umount(self):
        dev = _device(mountpoints=["/media/grey/MyTunes"])
        assert build_unmount_commands(dev) == [["umount", "/media/grey/MyTunes"]]

    def test_multiple_mountpoints_each_get_their_own_umount(self):
        dev = _device(mountpoints=["/media/grey/MyTunes", "/mnt/other"])
        assert build_unmount_commands(dev) == [
            ["umount", "/media/grey/MyTunes"],
            ["umount", "/mnt/other"],
        ]

    def test_does_not_execute_anything(self, monkeypatch):
        called = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
        build_unmount_commands(_device(mountpoints=["/media/grey/MyTunes"]))
        assert called == []


class TestWaitForPartition:
    """2026-09-03: a real run hit mkfs.exfat failing with "open failed :
    /dev/sde1, No such file or directory" right after parted returned --
    the partition table change is picked up by the kernel immediately but
    udev creates the /dev/sdXN node asynchronously, and mkfs.exfat ran
    before it existed."""

    def test_returns_immediately_if_the_node_already_exists(self, monkeypatch):
        monkeypatch.setattr(Path, "exists", lambda self: True)
        slept = []
        monkeypatch.setattr(usb_mod.time, "sleep", lambda s: slept.append(s))
        wait_for_partition("/dev/sdz1")
        assert slept == []

    def test_polls_until_the_node_appears(self, monkeypatch):
        calls = {"n": 0}

        def _exists(self):
            calls["n"] += 1
            return calls["n"] >= 3

        monkeypatch.setattr(Path, "exists", _exists)
        monkeypatch.setattr(usb_mod.time, "sleep", lambda s: None)
        wait_for_partition("/dev/sdz1")
        assert calls["n"] == 3

    def test_raises_a_clear_error_if_it_never_appears(self, monkeypatch):
        monkeypatch.setattr(Path, "exists", lambda self: False)
        monkeypatch.setattr(usb_mod.time, "sleep", lambda s: None)
        # A monotonic clock that jumps straight past the deadline on the
        # second read, so the test doesn't burn real wall-clock time.
        ticks = iter([0.0, 999.0])
        monkeypatch.setattr(usb_mod.time, "monotonic", lambda: next(ticks))
        with pytest.raises(RuntimeError, match="did not appear"):
            wait_for_partition("/dev/sdz1", timeout=10.0)


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


# ── main(): end-to-end orchestration ─────────────────────────────────────────
#
# Every dependency main() calls (get_config, device listing, denylist,
# confirm_wipe, execute_commands, mount/umount, copy_with_verification) is
# individually unit-tested above already -- these tests exist to prove
# main() *wires them together correctly and in the right order*, not to
# re-verify any one piece's own internal correctness. No real device, no
# real /mnt/ directory, no real subprocess: subprocess.run and Path.mkdir
# are both monkeypatched for every execute-path test here.


def _cfg(tmp_path: Path) -> MusicConfig:
    return MusicConfig(
        vault_root=tmp_path / "vault",
        inbox=tmp_path / "vault" / "INBOX",
        staging=tmp_path / "vault" / "STAGING",
        quarantine=tmp_path / "vault" / "QUARANTINE",
        runs_root=tmp_path / "vault" / "RUNS",
        meta_dir=tmp_path / "vault" / "MetaData",
        alac_library=tmp_path / "vault" / "ALAC-Library",
        db_path=tmp_path / "vault" / "musaeus.db",
    )


def _entries_resolve(playlist_file: Path) -> bool:
    """Every non-comment entry in *playlist_file*, resolved the way a player
    resolves it -- from the playlist's OWN directory -- points at a file that
    exists. This is the check that would have caught both 2026-09-05 path
    faults; asserting on the emitted text did not."""
    for line in playlist_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if not (playlist_file.parent / line).resolve().exists():
            return False
    return True


class TestPlaylistCopy:
    """2026-08-19 fix: transfer_to_usb.py never copied vault_root/Playlists/
    at all. Playlists mix ALAC-Library and AAC-Car entries in one file and
    use vault-relative paths, but the device copy is flat -- each playlist
    must be filtered to only its own library's tracks and rewritten to the
    flat relative form matching where files actually land."""

    def test_mixed_playlist_filtered_to_alac_only(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.ensure_dirs()
        alac_track = cfg.alac_library / "2026-08-17" / "Artist" / "Album" / "Song.m4a"
        alac_track.parent.mkdir(parents=True)
        alac_track.write_bytes(b"x")
        (cfg.vault_root / "Playlists").mkdir(parents=True)
        content = (
            "#EXTM3U\n"
            "#EXTINF:-1,Artist - Song\n"
            "../ALAC-Library/2026-08-17/Artist/Album/Song.m4a\n"
            "#EXTINF:-1,Other Artist - Other Song\n"
            "../RUNS/AAC-Car-Masked/_output/2026-08-17/Other Artist/Album/Other Song.m4a\n"
        )
        (cfg.vault_root / "Playlists" / "Rock.m3u8").write_text(content)

        dest = tmp_path / "device"
        dest.mkdir()
        # Stage the audio where copy_with_verification would have put it.
        # Without this the resolution assertion below could only ever fail,
        # and would be measuring the fixture rather than the rewrite.
        device_track = dest / "2026-08-17" / "Artist" / "Album" / "Song.m4a"
        device_track.parent.mkdir(parents=True)
        device_track.write_bytes(b"x")

        written = copy_playlists(cfg.vault_root, cfg.alac_library, dest)

        assert written == ["Rock.m3u8"]
        out_file = dest / "Playlists" / "Rock.m3u8"
        out = out_file.read_text()
        assert "Artist - Song" in out
        assert "Other Artist - Other Song" not in out
        assert "2026-08-17/Artist/Album/Song.m4a" in out
        # The vault-relative "../ALAC-Library/" prefix is gone...
        assert "ALAC-Library" not in out
        # ...but the entry still has to climb out of Playlists/ to reach the
        # audio. This asserted `"../" not in out` until 2026-09-05, which is
        # how an unresolvable playlist passed its own test for two weeks.
        assert _entries_resolve(out_file)

    def test_playlist_with_no_matching_tracks_is_skipped(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.ensure_dirs()
        (cfg.vault_root / "Playlists").mkdir(parents=True)
        content = (
            "#EXTM3U\n"
            "#EXTINF:-1,Other Artist - Other Song\n"
            "../RUNS/AAC-Car-Masked/_output/2026-08-17/Other Artist/Album/Other Song.m4a\n"
        )
        (cfg.vault_root / "Playlists" / "Jazz.m3u8").write_text(content)

        dest = tmp_path / "device"
        dest.mkdir()
        written = copy_playlists(cfg.vault_root, cfg.alac_library, dest)

        assert written == []
        assert not (dest / "Playlists" / "Jazz.m3u8").exists()

    def test_no_playlists_dir_returns_empty(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.ensure_dirs()
        dest = tmp_path / "device"
        dest.mkdir()
        assert copy_playlists(cfg.vault_root, cfg.alac_library, dest) == []

    def test_all_m3u8_all_tracks_of_source_included(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.ensure_dirs()
        for name in ("a.m4a", "b.m4a"):
            p = cfg.alac_library / "2026-08-17" / "Artist" / "Album" / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
        (cfg.vault_root / "Playlists").mkdir(parents=True)
        content = (
            "#EXTM3U\n"
            "#EXTINF:-1,Artist - A\n"
            "../ALAC-Library/2026-08-17/Artist/Album/a.m4a\n"
            "#EXTINF:-1,Artist - B\n"
            "../ALAC-Library/2026-08-17/Artist/Album/b.m4a\n"
        )
        (cfg.vault_root / "Playlists" / "All.m3u8").write_text(content)

        dest = tmp_path / "device"
        dest.mkdir()
        written = copy_playlists(cfg.vault_root, cfg.alac_library, dest)

        assert written == ["All.m3u8"]
        out = (dest / "Playlists" / "All.m3u8").read_text()
        assert out.count("#EXTINF") == 2


class TestSourceDir:
    def test_alac_points_at_alac_library(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert usb_mod._source_dir("alac", cfg) == cfg.alac_library

    def test_car_points_at_the_published_car_library(self, tmp_path):
        """Was cfg.runs_root / "AAC-Car-Masked" / "_output" -- the
        build/staging area -- until 2026-09-03, when the built-and-masked
        edition started being published to cfg.car_library. Pointing this
        at the old staging path would transfer the wrong audio: its
        encoded/ subtree is the pre-masking intermediate (no car-cabin
        noise mixed in), kept only so a re-run can skip already-encoded
        files."""
        cfg = _cfg(tmp_path)
        assert usb_mod._source_dir("car", cfg) == cfg.car_library

    def test_unknown_library_raises(self, tmp_path):
        cfg = _cfg(tmp_path)
        with pytest.raises(ValueError):
            usb_mod._source_dir("cassette", cfg)


class TestMainEndToEnd:
    @pytest.fixture(autouse=True)
    def _mkfs_present(self, monkeypatch):
        """check_mkfs_available() really does look for mkfs.exfat/mkfs.vfat on
        the machine running the tests. Stub it here so these tests keep
        proving wiring rather than which packages this host has installed --
        TestFormatTooling covers the check itself."""
        monkeypatch.setattr(usb_mod, "check_mkfs_available", lambda filesystem: None)

    def test_list_devices_prints_and_returns_0(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["prog", "--list-devices"])
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [_device(path="/dev/sdz")])
        assert usb_mod.main() == 0
        assert "/dev/sdz" in capsys.readouterr().out

    def test_missing_library_errors(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["prog"])
        with pytest.raises(SystemExit):
            usb_mod.main()

    def test_source_not_found_returns_1(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(
            sys,
            "argv",
            ["prog", "--library", "alac", "--source-dir", str(tmp_path / "nope")],
        )
        assert usb_mod.main() == 1

    def test_device_not_found_among_removable_returns_1(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        (cfg.alac_library).mkdir(parents=True)
        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [_device(path="/dev/sdz")])
        monkeypatch.setattr(
            sys, "argv", ["prog", "--library", "alac", "--device", "/dev/does_not_exist"]
        )
        assert usb_mod.main() == 1

    def test_denylisted_device_refuses_even_in_dry_run(self, tmp_path, monkeypatch):
        """The denylist check happens before the dry-run/execute branch --
        a denylisted device must be refused even for a dry run, not just
        for --execute."""
        cfg = _cfg(tmp_path)
        cfg.alac_library.mkdir(parents=True)
        dev = _device(path="/dev/sda")
        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [dev])
        monkeypatch.setattr(usb_mod, "critical_backing_disks", lambda *a, **k: {"/dev/sda"})
        monkeypatch.setattr(sys, "argv", ["prog", "--library", "alac", "--device", "/dev/sda"])

        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
        assert usb_mod.main() == 1
        assert calls == []

    def test_dry_run_never_calls_destructive_subprocess(self, tmp_path, monkeypatch, capsys):
        cfg = _cfg(tmp_path)
        cfg.alac_library.mkdir(parents=True)
        (cfg.alac_library / "track.m4a").write_bytes(b"x")
        dev = _device(path="/dev/sdz")
        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [dev])
        monkeypatch.setattr(usb_mod, "critical_backing_disks", lambda *a, **k: set())
        monkeypatch.setattr(sys, "argv", ["prog", "--library", "alac", "--device", "/dev/sdz"])

        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
        assert usb_mod.main() == 0
        assert calls == []
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "would run: wipefs" in out
        assert "would copy 1 file(s)" in out

    def test_execute_wires_everything_in_order(self, tmp_path, monkeypatch, capsys):
        cfg = _cfg(tmp_path)
        cfg.alac_library.mkdir(parents=True)
        (cfg.alac_library / "track.m4a").write_bytes(b"x")
        dev = _device(path="/dev/sdz")

        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [dev])
        monkeypatch.setattr(usb_mod, "critical_backing_disks", lambda *a, **k: set())
        monkeypatch.setattr(usb_mod, "confirm_wipe", lambda device: True)
        monkeypatch.setattr(usb_mod.os, "geteuid", lambda: 0)
        monkeypatch.setattr(usb_mod.Path, "mkdir", lambda self, **kw: None)
        monkeypatch.setattr(usb_mod.Path, "exists", lambda self: True)

        subprocess_calls = []

        def _fake_run(cmd, **kwargs):
            subprocess_calls.append(cmd)

            class _R:
                returncode = 0
                stderr = ""

            return _R()

        monkeypatch.setattr(subprocess, "run", _fake_run)

        copy_calls = []

        def _fake_copy(files, source_root, dest_root, **kwargs):
            copy_calls.append((files, source_root, dest_root))
            return CopyResult(ok=[str(f) for f in files])

        monkeypatch.setattr(usb_mod, "copy_with_verification", _fake_copy)
        monkeypatch.setattr(
            sys, "argv", ["prog", "--library", "alac", "--device", "/dev/sdz", "--execute"]
        )

        assert usb_mod.main() == 0

        # wipefs -> parted (x2) -> mkfs.exfat -> mount -> umount, in order.
        cmd_names = [c[0] for c in subprocess_calls]
        assert cmd_names == ["wipefs", "parted", "parted", "mkfs.exfat", "mount", "umount"]
        assert subprocess_calls[4][0:2] == ["mount", "/dev/sdz1"]
        assert subprocess_calls[5][0] == "umount"

        assert len(copy_calls) == 1
        files, source_root, dest_root = copy_calls[0]
        assert source_root == cfg.alac_library
        assert str(dest_root).startswith("/mnt/musaeus_usb_")

        out = capsys.readouterr().out
        assert "1 copied+verified, 0 failed" in out

    def test_dry_run_previews_the_unmount_when_device_is_mounted(
        self, tmp_path, monkeypatch, capsys
    ):
        cfg = _cfg(tmp_path)
        cfg.alac_library.mkdir(parents=True)
        (cfg.alac_library / "track.m4a").write_bytes(b"x")
        dev = _device(path="/dev/sdz", mountpoints=["/media/grey/MyTunes"])
        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [dev])
        monkeypatch.setattr(usb_mod, "critical_backing_disks", lambda *a, **k: set())
        monkeypatch.setattr(sys, "argv", ["prog", "--library", "alac", "--device", "/dev/sdz"])

        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
        assert usb_mod.main() == 0
        assert calls == []
        out = capsys.readouterr().out
        assert "would run: umount /media/grey/MyTunes" in out

    def test_execute_unmounts_a_busy_device_before_wiping(self, tmp_path, monkeypatch):
        """The real failure this guards against: wipefs refuses on a
        mounted device with "Device or resource busy". umount must run,
        and must run before wipefs, not after."""
        cfg = _cfg(tmp_path)
        cfg.alac_library.mkdir(parents=True)
        (cfg.alac_library / "track.m4a").write_bytes(b"x")
        dev = _device(path="/dev/sdz", mountpoints=["/media/grey/MyTunes"])

        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [dev])
        monkeypatch.setattr(usb_mod, "critical_backing_disks", lambda *a, **k: set())
        monkeypatch.setattr(usb_mod, "confirm_wipe", lambda device: True)
        monkeypatch.setattr(usb_mod.os, "geteuid", lambda: 0)
        monkeypatch.setattr(usb_mod.Path, "mkdir", lambda self, **kw: None)
        monkeypatch.setattr(usb_mod.Path, "exists", lambda self: True)

        subprocess_calls = []

        def _fake_run(cmd, **kwargs):
            subprocess_calls.append(cmd)

            class _R:
                returncode = 0
                stderr = ""

            return _R()

        monkeypatch.setattr(subprocess, "run", _fake_run)
        monkeypatch.setattr(
            usb_mod, "copy_with_verification",
            lambda files, source_root, dest_root, **kw: CopyResult(ok=[str(f) for f in files]),
        )
        monkeypatch.setattr(
            sys, "argv", ["prog", "--library", "alac", "--device", "/dev/sdz", "--execute"]
        )

        assert usb_mod.main() == 0

        cmd_names = [c[0] for c in subprocess_calls]
        assert cmd_names[0] == "umount", "must unmount before wipefs, not after"
        assert cmd_names[1] == "wipefs"
        assert subprocess_calls[0] == ["umount", "/media/grey/MyTunes"]

    def test_execute_unmounts_using_freshly_refetched_mount_state(self, tmp_path, monkeypatch):
        """Mount state can change between the initial listing and the
        moment execution actually starts (same reasoning as the existing
        denylist recheck) -- the unmount must use a fresh re-fetch, not
        the mountpoints captured before the typed confirmation."""
        cfg = _cfg(tmp_path)
        cfg.alac_library.mkdir(parents=True)
        (cfg.alac_library / "track.m4a").write_bytes(b"x")
        stale = _device(path="/dev/sdz", mountpoints=[])
        fresh = _device(path="/dev/sdz", mountpoints=["/media/grey/MyTunes"])

        listings = iter([[stale], [fresh]])
        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: next(listings))
        monkeypatch.setattr(usb_mod, "critical_backing_disks", lambda *a, **k: set())
        monkeypatch.setattr(usb_mod, "confirm_wipe", lambda device: True)
        monkeypatch.setattr(usb_mod.os, "geteuid", lambda: 0)
        monkeypatch.setattr(usb_mod.Path, "mkdir", lambda self, **kw: None)
        monkeypatch.setattr(usb_mod.Path, "exists", lambda self: True)

        subprocess_calls = []

        def _fake_run(cmd, **kwargs):
            subprocess_calls.append(cmd)

            class _R:
                returncode = 0
                stderr = ""

            return _R()

        monkeypatch.setattr(subprocess, "run", _fake_run)
        monkeypatch.setattr(
            usb_mod, "copy_with_verification",
            lambda files, source_root, dest_root, **kw: CopyResult(ok=[str(f) for f in files]),
        )
        monkeypatch.setattr(
            sys, "argv", ["prog", "--library", "alac", "--device", "/dev/sdz", "--execute"]
        )

        assert usb_mod.main() == 0
        assert subprocess_calls[0] == ["umount", "/media/grey/MyTunes"]

    def test_execute_still_umounts_after_copy_raises(self, tmp_path, monkeypatch):
        """finally: must run even if copy_with_verification itself blows up --
        never leave the device mounted on an unhandled error."""
        cfg = _cfg(tmp_path)
        cfg.alac_library.mkdir(parents=True)
        (cfg.alac_library / "track.m4a").write_bytes(b"x")
        dev = _device(path="/dev/sdz")

        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [dev])
        monkeypatch.setattr(usb_mod, "critical_backing_disks", lambda *a, **k: set())
        monkeypatch.setattr(usb_mod, "confirm_wipe", lambda device: True)
        monkeypatch.setattr(usb_mod.os, "geteuid", lambda: 0)
        monkeypatch.setattr(usb_mod.Path, "mkdir", lambda self, **kw: None)
        monkeypatch.setattr(usb_mod.Path, "exists", lambda self: True)

        subprocess_calls = []

        def _fake_run(cmd, **kwargs):
            subprocess_calls.append(cmd)

            class _R:
                returncode = 0
                stderr = ""

            return _R()

        monkeypatch.setattr(subprocess, "run", _fake_run)

        def _raising_copy(*a, **k):
            raise RuntimeError("simulated crash mid-copy")

        monkeypatch.setattr(usb_mod, "copy_with_verification", _raising_copy)
        monkeypatch.setattr(
            sys, "argv", ["prog", "--library", "alac", "--device", "/dev/sdz", "--execute"]
        )

        with pytest.raises(RuntimeError):
            usb_mod.main()

        assert subprocess_calls[-1][0] == "umount"

    def test_confirm_wipe_false_aborts_before_any_wipe_command(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        cfg.alac_library.mkdir(parents=True)
        dev = _device(path="/dev/sdz")

        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [dev])
        monkeypatch.setattr(usb_mod, "critical_backing_disks", lambda *a, **k: set())
        monkeypatch.setattr(usb_mod, "confirm_wipe", lambda device: False)
        monkeypatch.setattr(
            sys, "argv", ["prog", "--library", "alac", "--device", "/dev/sdz", "--execute"]
        )

        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
        assert usb_mod.main() == 1
        assert calls == []

    def test_recheck_denylist_after_confirmation_blocks_execute(self, tmp_path, monkeypatch):
        """Mount state can change between the initial denylist check and the
        typed confirmation completing -- the post-confirmation re-check must
        still be able to block the run."""
        cfg = _cfg(tmp_path)
        cfg.alac_library.mkdir(parents=True)
        dev = _device(path="/dev/sdz")

        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [dev])
        monkeypatch.setattr(usb_mod, "confirm_wipe", lambda device: True)

        results = iter([set(), {"/dev/sdz"}])
        monkeypatch.setattr(usb_mod, "critical_backing_disks", lambda *a, **k: next(results))
        monkeypatch.setattr(
            sys, "argv", ["prog", "--library", "alac", "--device", "/dev/sdz", "--execute"]
        )

        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
        assert usb_mod.main() == 1
        assert calls == []

    def test_denylist_resolution_error_before_execute_refuses(self, tmp_path, monkeypatch):
        """If critical_backing_disks can't resolve a critical path (findmnt
        failure etc.), main() must refuse the run entirely -- fail closed,
        not proceed as if nothing were critical."""
        cfg = _cfg(tmp_path)
        cfg.alac_library.mkdir(parents=True)
        dev = _device(path="/dev/sdz")

        def _raise(*a, **k):
            raise usb_mod.DenylistResolutionError("could not resolve /home")

        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [dev])
        monkeypatch.setattr(usb_mod, "critical_backing_disks", _raise)
        monkeypatch.setattr(sys, "argv", ["prog", "--library", "alac", "--device", "/dev/sdz"])

        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
        assert usb_mod.main() == 1
        assert calls == []

    def test_denylist_resolution_error_on_recheck_refuses(self, tmp_path, monkeypatch):
        """Same failure, but surfacing only on the post-confirmation
        re-check -- must still refuse rather than proceed."""
        cfg = _cfg(tmp_path)
        cfg.alac_library.mkdir(parents=True)
        dev = _device(path="/dev/sdz")

        calls_to_denylist = {"n": 0}

        def _flaky(*a, **k):
            calls_to_denylist["n"] += 1
            if calls_to_denylist["n"] == 1:
                return set()
            raise usb_mod.DenylistResolutionError("mount state changed mid-prompt")

        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [dev])
        monkeypatch.setattr(usb_mod, "critical_backing_disks", _flaky)
        monkeypatch.setattr(usb_mod, "confirm_wipe", lambda device: True)
        monkeypatch.setattr(
            sys, "argv", ["prog", "--library", "alac", "--device", "/dev/sdz", "--execute"]
        )

        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
        assert usb_mod.main() == 1
        assert calls == []

    def test_interactive_device_picker_used_when_no_device_flag(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        cfg.alac_library.mkdir(parents=True)
        dev = _device(path="/dev/sdz")

        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [dev])
        monkeypatch.setattr(usb_mod, "critical_backing_disks", lambda *a, **k: set())
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "/dev/sdz")
        monkeypatch.setattr(sys, "argv", ["prog", "--library", "alac"])

        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
        assert usb_mod.main() == 0  # dry run, valid device chosen interactively
        assert calls == []

    def test_interactive_picker_no_tty_refuses(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        cfg.alac_library.mkdir(parents=True)
        dev = _device(path="/dev/sdz")

        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [dev])
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(sys, "argv", ["prog", "--library", "alac"])
        assert usb_mod.main() == 1

    def test_interactive_picker_unknown_choice_refuses(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        cfg.alac_library.mkdir(parents=True)
        dev = _device(path="/dev/sdz")

        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [dev])
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: "/dev/does_not_exist")
        monkeypatch.setattr(sys, "argv", ["prog", "--library", "alac"])
        assert usb_mod.main() == 1

    def test_execute_reports_and_returns_1_on_copy_failures(self, tmp_path, monkeypatch, capsys):
        cfg = _cfg(tmp_path)
        cfg.alac_library.mkdir(parents=True)
        (cfg.alac_library / "track.m4a").write_bytes(b"x")
        dev = _device(path="/dev/sdz")

        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [dev])
        monkeypatch.setattr(usb_mod, "critical_backing_disks", lambda *a, **k: set())
        monkeypatch.setattr(usb_mod, "confirm_wipe", lambda device: True)
        monkeypatch.setattr(usb_mod.os, "geteuid", lambda: 0)
        monkeypatch.setattr(usb_mod.Path, "mkdir", lambda self, **kw: None)
        monkeypatch.setattr(usb_mod.Path, "exists", lambda self: True)
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stderr": ""})()
        )
        monkeypatch.setattr(
            usb_mod,
            "copy_with_verification",
            lambda *a, **k: CopyResult(
                ok=[], failed=[("/some/file.m4a", "post-copy hash mismatch")]
            ),
        )
        monkeypatch.setattr(
            sys, "argv", ["prog", "--library", "alac", "--device", "/dev/sdz", "--execute"]
        )

        assert usb_mod.main() == 1
        out = capsys.readouterr().out
        assert "0 copied+verified, 1 failed" in out
        assert "FAILED /some/file.m4a: post-copy hash mismatch" in out


# ── Filesystem choice (2026-09-05) ───────────────────────────────────────────
#
# 2026-09-04: Grey's Android head unit rejected a GPT+ExFAT stick as "not set
# up for Android" and reformatted it itself, erasing the transfer. These tests
# pin the pairing the head unit chose for itself.


class TestPartitionPathFor:
    def test_plain_disk_gets_a_bare_digit(self):
        assert partition_path_for("/dev/sdb") == "/dev/sdb1"

    def test_nvme_style_gets_a_p_separator(self):
        assert partition_path_for("/dev/nvme0n1") == "/dev/nvme0n1p1"


class TestFilesystemFormatCommands:
    def test_fat32_uses_msdos_table_and_mkfs_vfat(self):
        cmds = build_wipe_and_format_commands("/dev/sdb", filesystem="fat32")
        assert cmds == [
            ["wipefs", "--all", "/dev/sdb"],
            ["parted", "--script", "/dev/sdb", "mklabel", "msdos"],
            ["parted", "--script", "/dev/sdb", "mkpart", "primary", "fat32", "0%", "100%"],
            ["mkfs.vfat", "-F", "32", "-n", "MUSAEUS", "/dev/sdb1"],
        ]

    def test_exfat_remains_gpt_and_mkfs_exfat(self):
        cmds = build_wipe_and_format_commands("/dev/sdb", filesystem="exfat")
        assert cmds == [
            ["wipefs", "--all", "/dev/sdb"],
            ["parted", "--script", "/dev/sdb", "mklabel", "gpt"],
            ["parted", "--script", "/dev/sdb", "mkpart", "primary", "0%", "100%"],
            ["mkfs.exfat", "-n", "MUSAEUS", "/dev/sdb1"],
        ]

    def test_default_is_still_exfat_for_positional_callers(self):
        """The signature grew a third parameter; the two existing positional
        call sites must keep meaning what they meant."""
        assert build_wipe_and_format_commands("/dev/sdb") == build_wipe_and_format_commands(
            "/dev/sdb", "MUSAEUS", "exfat"
        )

    def test_fat32_mkpart_carries_the_fs_type_argument(self):
        """Not cosmetic: on an msdos table, parted's fs-type argument is what
        sets the partition type byte to 0x0c (W95 FAT32 LBA). Verified
        2026-09-05 against a 256 MB file image -- with "fat32" fdisk reports
        "c W95 FAT32 (LBA)". Without it parted leaves 0x83 (Linux), and a head
        unit that reads the partition table before the filesystem can reject
        the stick on that alone."""
        mkpart = build_wipe_and_format_commands("/dev/sdb", filesystem="fat32")[2]
        assert mkpart == [
            "parted", "--script", "/dev/sdb", "mkpart", "primary", "fat32", "0%", "100%"
        ]

    def test_does_not_execute_anything_for_either_filesystem(self, monkeypatch):
        called = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
        build_wipe_and_format_commands("/dev/sdb", filesystem="fat32")
        build_wipe_and_format_commands("/dev/sdb", filesystem="exfat")
        assert called == []


class TestLabelValidation:
    def test_twelve_char_fat32_label_rejected(self):
        """Measured 2026-09-05: mkfs.vfat -F 32 -n MUSAEUSTOOLONG exits 1 with
        "Label can be no longer than 11 characters" and writes nothing. It is
        the last of the four commands, so without this guard the stick is
        already wiped and repartitioned when that happens."""
        with pytest.raises(ValueError, match="11"):
            validate_label("MUSAEUSTOOLONG", "fat32")

    def test_eleven_char_fat32_label_accepted(self):
        assert validate_label("MUSAEUSCAR2", "fat32") is None

    def test_lowercase_fat32_label_rejected(self):
        """Stricter than mkfs.vfat on purpose -- measured 2026-09-05, it exits
        0 with only a "might not work properly on some systems" warning and
        writes the lowercase label. The target is a head unit that has already
        rejected one stick."""
        with pytest.raises(ValueError, match="uppercase"):
            validate_label("musaeus", "fat32")

    def test_exfat_labels_are_not_length_limited(self):
        assert validate_label("A_MUCH_LONGER_LABEL", "exfat") is None

    def test_long_label_is_refused_by_the_builder_not_just_the_validator(self, monkeypatch):
        """The guard has to sit on the path that emits commands, not beside
        it -- and it must refuse before emitting the wipefs that precedes it."""
        called = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
        with pytest.raises(ValueError):
            build_wipe_and_format_commands("/dev/sdb", "MUSAEUSTOOLONG", "fat32")
        assert called == []


class TestFat32FileSizeGuard:
    def test_files_under_the_cap_are_not_flagged(self, tmp_path):
        f = tmp_path / "track.m4a"
        f.write_bytes(b"x" * 1024)
        assert files_too_big_for_fat32([f]) == []

    def test_a_file_at_or_over_4_gib_is_flagged(self, tmp_path, monkeypatch):
        f = tmp_path / "huge.m4a"
        f.write_bytes(b"x")
        # Sparse-file the size rather than writing 4 GiB during a test run.
        real_stat = Path.stat
        monkeypatch.setattr(
            Path,
            "stat",
            lambda self, **kw: type("S", (), {"st_size": 4 * 1024**3})()
            if self == f
            else real_stat(self, **kw),
        )
        flagged = files_too_big_for_fat32([f])
        assert [path for path, _ in flagged] == [f]

    def test_unreadable_file_is_skipped_not_crashed_on(self, tmp_path):
        assert files_too_big_for_fat32([tmp_path / "does_not_exist.m4a"]) == []


class TestFormatTooling:
    def test_missing_mkfs_raises_with_an_apt_install_hint(self, monkeypatch):
        monkeypatch.setattr(usb_mod, "_which_including_sbin", lambda cmd: None)
        with pytest.raises(FormatToolingError, match="apt install dosfstools"):
            check_mkfs_available("fat32")
        with pytest.raises(FormatToolingError, match="apt install exfatprogs"):
            check_mkfs_available("exfat")

    def test_present_mkfs_passes(self, monkeypatch):
        monkeypatch.setattr(usb_mod, "_which_including_sbin", lambda cmd: "/usr/sbin/" + cmd)
        assert check_mkfs_available("fat32") is None

    def test_lookup_searches_sbin_which_is_off_a_normal_users_path(self, monkeypatch):
        """Measured 2026-09-05: mkfs.vfat and mkfs.exfat are both installed on
        this machine and both live in /usr/sbin, which Debian keeps off a
        non-root user's PATH. The dry run is run unprivileged (console.py only
        adds sudo for --execute), so a plain shutil.which() would have told
        Grey to install a package he already had -- on exactly the car
        transfer this change exists to enable."""
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        seen = []

        def _fake_which(cmd, path=None):
            seen.append(path)
            return "/usr/sbin/mkfs.vfat" if path and "/usr/sbin" in path else None

        monkeypatch.setattr(usb_mod.shutil, "which", _fake_which)
        assert usb_mod._which_including_sbin("mkfs.vfat") == "/usr/sbin/mkfs.vfat"
        assert any(pth and "/usr/sbin" in pth for pth in seen)


class TestFilesystemDefaultsEndToEnd:
    """The defaults are only real if main() applies them. These drive main()
    in dry run and read back the command lines it actually prints."""

    def _run(self, tmp_path, monkeypatch, capsys, argv, library_dir="car_library"):
        cfg = _cfg(tmp_path)
        for lib in (cfg.alac_library, cfg.car_library):
            lib.mkdir(parents=True, exist_ok=True)
            (lib / "track.m4a").write_bytes(b"x")
        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [_device(path="/dev/sdz")])
        monkeypatch.setattr(usb_mod, "critical_backing_disks", lambda *a, **k: set())
        monkeypatch.setattr(usb_mod, "check_mkfs_available", lambda filesystem: None)
        monkeypatch.setattr(sys, "argv", ["prog"] + argv)
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
        rc = usb_mod.main()
        return rc, capsys.readouterr().out, calls

    def test_car_defaults_to_fat32_on_msdos(self, tmp_path, monkeypatch, capsys):
        rc, out, calls = self._run(
            tmp_path, monkeypatch, capsys, ["--library", "car", "--device", "/dev/sdz"]
        )
        assert rc == 0
        assert "would run: parted --script /dev/sdz mklabel msdos" in out
        assert "would run: mkfs.vfat -F 32 -n MUSAEUS /dev/sdz1" in out
        assert "mklabel gpt" not in out
        assert "mkfs.exfat" not in out
        assert calls == []

    def test_alac_defaults_to_exfat_on_gpt(self, tmp_path, monkeypatch, capsys):
        rc, out, calls = self._run(
            tmp_path, monkeypatch, capsys, ["--library", "alac", "--device", "/dev/sdz"]
        )
        assert rc == 0
        assert "would run: parted --script /dev/sdz mklabel gpt" in out
        assert "would run: mkfs.exfat -n MUSAEUS /dev/sdz1" in out
        assert "mklabel msdos" not in out
        assert "mkfs.vfat" not in out
        assert calls == []

    def test_car_with_explicit_exfat_honours_the_override(self, tmp_path, monkeypatch, capsys):
        rc, out, calls = self._run(
            tmp_path,
            monkeypatch,
            capsys,
            ["--library", "car", "--filesystem", "exfat", "--device", "/dev/sdz"],
        )
        assert rc == 0
        assert "would run: parted --script /dev/sdz mklabel gpt" in out
        assert "would run: mkfs.exfat -n MUSAEUS /dev/sdz1" in out
        assert "mkfs.vfat" not in out

    def test_alac_with_explicit_fat32_honours_the_override(self, tmp_path, monkeypatch, capsys):
        rc, out, calls = self._run(
            tmp_path,
            monkeypatch,
            capsys,
            ["--library", "alac", "--filesystem", "fat32", "--device", "/dev/sdz"],
        )
        assert rc == 0
        assert "would run: parted --script /dev/sdz mklabel msdos" in out
        assert "would run: mkfs.vfat -F 32 -n MUSAEUS /dev/sdz1" in out

    def test_oversized_file_refuses_fat32_before_any_destructive_command(
        self, tmp_path, monkeypatch, capsys
    ):
        """The check has to happen before the wipe, not during the copy --
        discovering it mid-copy means discovering it on an erased stick."""
        monkeypatch.setattr(
            usb_mod, "files_too_big_for_fat32", lambda files: [(Path("/src/huge.m4a"), 5 * 1024**3)]
        )
        rc, out, calls = self._run(
            tmp_path, monkeypatch, capsys, ["--library", "car", "--device", "/dev/sdz"]
        )
        assert rc == 1
        assert "would run" not in out  # not one command line was emitted
        assert calls == []

    def test_missing_mkfs_refuses_before_any_destructive_command(
        self, tmp_path, monkeypatch, capsys
    ):
        cfg = _cfg(tmp_path)
        cfg.car_library.mkdir(parents=True, exist_ok=True)
        (cfg.car_library / "track.m4a").write_bytes(b"x")
        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [_device(path="/dev/sdz")])
        monkeypatch.setattr(usb_mod, "critical_backing_disks", lambda *a, **k: set())
        monkeypatch.setattr(usb_mod, "_which_including_sbin", lambda cmd: None)
        monkeypatch.setattr(sys, "argv", ["prog", "--library", "car", "--device", "/dev/sdz"])
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
        assert usb_mod.main() == 1
        assert calls == []
        assert "would run" not in capsys.readouterr().out


class TestFat32ExecutePath:
    """The FAT32 wiring on the path that actually wipes. Everything else
    about fat32 is proven in dry run; this is the one that has no rollback."""

    def test_execute_runs_mkfs_vfat_after_the_udev_wait(self, tmp_path, monkeypatch, capsys):
        """mkfs must run AFTER wait_for_partition, not with the parted calls.

        The split is positional -- execute_commands(cmds[:-1]), wait,
        execute_commands(cmds[-1:]) -- so appending a fifth command to
        build_wipe_and_format_commands would silently move mkfs into the
        pre-wait batch and reintroduce the udev race wait_for_partition
        exists to prevent (hit for real 2026-09-03: "open failed :
        /dev/sde1, No such file or directory"). Asserting the wait's
        position in the same sequence is what catches that.

        The argv asserted here is the one verified against a 256 MB file
        image on 2026-09-05: it produces a real FAT32 filesystem labelled
        MUSAEUS, on a partition of type 0x0c."""
        cfg = _cfg(tmp_path)
        cfg.car_library.mkdir(parents=True, exist_ok=True)
        (cfg.car_library / "track.m4a").write_bytes(b"x")
        dev = _device(path="/dev/sdz")

        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [dev])
        monkeypatch.setattr(usb_mod, "critical_backing_disks", lambda *a, **k: set())
        monkeypatch.setattr(usb_mod, "check_mkfs_available", lambda filesystem: None)
        monkeypatch.setattr(usb_mod, "confirm_wipe", lambda device: True)
        monkeypatch.setattr(usb_mod.os, "geteuid", lambda: 0)
        monkeypatch.setattr(usb_mod.Path, "mkdir", lambda self, **kw: None)
        monkeypatch.setattr(usb_mod.Path, "exists", lambda self: True)

        calls = []

        def _fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _FakeCompleted()

        monkeypatch.setattr(subprocess, "run", _fake_run)
        # Recorded into the same sequence so its ORDER is asserted, not just
        # the fact that it was called.
        monkeypatch.setattr(
            usb_mod, "wait_for_partition", lambda p, **kw: calls.append(["<wait>", p])
        )
        monkeypatch.setattr(
            usb_mod,
            "copy_with_verification",
            lambda files, *a, **k: CopyResult(ok=[str(f) for f in files]),
        )
        monkeypatch.setattr(
            sys, "argv", ["prog", "--library", "car", "--device", "/dev/sdz", "--execute"]
        )

        assert usb_mod.main() == 0

        assert [c[0] for c in calls] == [
            "wipefs", "parted", "parted", "<wait>", "mkfs.vfat", "mount", "umount"
        ]
        assert calls[1] == ["parted", "--script", "/dev/sdz", "mklabel", "msdos"]
        assert calls[2] == [
            "parted", "--script", "/dev/sdz", "mkpart", "primary", "fat32", "0%", "100%"
        ]
        assert calls[3] == ["<wait>", "/dev/sdz1"]
        assert calls[4] == ["mkfs.vfat", "-F", "32", "-n", "MUSAEUS", "/dev/sdz1"]
        assert calls[5][0:2] == ["mount", "/dev/sdz1"]

    def test_execute_emits_exactly_what_the_dry_run_promised(self, tmp_path, monkeypatch, capsys):
        """A dry run is only worth running if it is the same command list.
        main() builds it once and reuses it; this proves the two agree
        rather than trusting that they were built the same way twice."""
        promised = []
        executed = []

        # Both fixtures are built BEFORE anything is monkeypatched -- the
        # first run stubs out Path.mkdir, and that stub would otherwise still
        # be in place when the second run tried to create its library.
        cfgs = {}
        for name in ("dry", "exec"):
            cfg = _cfg(tmp_path / name)
            cfg.car_library.mkdir(parents=True, exist_ok=True)
            (cfg.car_library / "track.m4a").write_bytes(b"x")
            cfgs[name] = cfg

        def _run_once(argv, sink, execute):
            cfg = cfgs["exec" if execute else "dry"]
            dev = _device(path="/dev/sdz")
            monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
            monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [dev])
            monkeypatch.setattr(usb_mod, "critical_backing_disks", lambda *a, **k: set())
            monkeypatch.setattr(usb_mod, "check_mkfs_available", lambda filesystem: None)
            monkeypatch.setattr(usb_mod, "confirm_wipe", lambda device: True)
            monkeypatch.setattr(usb_mod.os, "geteuid", lambda: 0)
            monkeypatch.setattr(usb_mod.Path, "mkdir", lambda self, **kw: None)
            monkeypatch.setattr(usb_mod.Path, "exists", lambda self: True)
            monkeypatch.setattr(usb_mod, "wait_for_partition", lambda p, **kw: None)
            monkeypatch.setattr(
                usb_mod, "copy_with_verification", lambda files, *a, **k: CopyResult(ok=[])
            )

            def _fake_run(cmd, **kwargs):
                sink.append(cmd)
                return _FakeCompleted()

            monkeypatch.setattr(subprocess, "run", _fake_run)
            monkeypatch.setattr(sys, "argv", ["prog"] + argv)
            usb_mod.main()

        base = ["--library", "car", "--device", "/dev/sdz"]
        _run_once(base, promised, execute=False)
        dry_lines = [
            ln.split("would run: ", 1)[1]
            for ln in capsys.readouterr().out.splitlines()
            if "would run: " in ln
        ]
        _run_once(base + ["--execute"], executed, execute=True)

        # Everything the dry run promised, in order, up to the mount step.
        assert dry_lines == [" ".join(c) for c in executed[: len(dry_lines)]]


# ── --no-format (2026-09-05) ─────────────────────────────────────────────────


def _no_format_env(tmp_path, monkeypatch, dev=None):
    """Config + a source library + a fake stick directory, with every gate
    that --no-format is allowed to skip wired to EXPLODE if it is reached."""
    cfg = _cfg(tmp_path)
    cfg.car_library.mkdir(parents=True, exist_ok=True)
    (cfg.car_library / "track.m4a").write_bytes(b"hello")
    stick = tmp_path / "stick"
    stick.mkdir(exist_ok=True)

    monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
    monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [dev or _device(path="/dev/sdz")])
    monkeypatch.setattr(usb_mod, "critical_backing_disks", lambda *a, **k: set())

    def _boom(*a, **k):
        raise AssertionError("--no-format reached a gate it must never reach")

    monkeypatch.setattr(usb_mod, "confirm_wipe", _boom)
    monkeypatch.setattr(usb_mod, "execute_commands", _boom)
    monkeypatch.setattr(usb_mod, "wait_for_partition", _boom)
    monkeypatch.setattr("builtins.input", _boom)
    # Unprivileged, and not a terminal -- the two conditions the wipe path
    # refuses under. --no-format must be unbothered by both.
    monkeypatch.setattr(usb_mod.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    return cfg, stick


class TestNoFormatEmitsNothingDestructive:
    def test_dry_run_emits_no_destructive_command(self, tmp_path, monkeypatch, capsys):
        cfg, stick = _no_format_env(tmp_path, monkeypatch)
        monkeypatch.setattr(
            sys, "argv", ["prog", "--library", "car", "--no-format", "--dest", str(stick)]
        )
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
        assert usb_mod.main() == 0
        assert calls == []
        out = capsys.readouterr().out
        # The banner names those commands to say it is NOT running them, so
        # assert on emitted command lines, not on the words appearing at all.
        assert "would run" not in out
        emitted = [ln for ln in out.splitlines() if "would run" in ln or "running:" in ln]
        assert emitted == []

    def test_execute_emits_no_destructive_command(self, tmp_path, monkeypatch, capsys):
        """The real one: --execute --no-format actually copies, and must still
        never unmount, wipe, partition, format or even mount anything."""
        cfg, stick = _no_format_env(tmp_path, monkeypatch)
        monkeypatch.setattr(
            sys,
            "argv",
            ["prog", "--library", "car", "--no-format", "--dest", str(stick), "--execute"],
        )
        calls = []

        def _record(cmd, *a, **k):
            calls.append(cmd)
            return _FakeCompleted()

        monkeypatch.setattr(subprocess, "run", _record)
        assert usb_mod.main() == 0

        emitted = {c[0] for c in calls if isinstance(c, list) and c}
        assert not (emitted & _DESTRUCTIVE), f"destructive command emitted: {emitted}"
        # And the copy really happened -- proving the assertion above is not
        # vacuously true because nothing ran at all.
        assert (stick / "track.m4a").read_bytes() == b"hello"
        assert "1 copied+verified, 0 failed" in capsys.readouterr().out

    def test_needs_execute_just_like_the_wipe_path(self, tmp_path, monkeypatch):
        """--no-format skips the typed confirmation and the root check, and
        ONLY those. Gate 5 still stands: no --execute, no bytes written."""
        cfg, stick = _no_format_env(tmp_path, monkeypatch)
        monkeypatch.setattr(
            sys, "argv", ["prog", "--library", "car", "--no-format", "--dest", str(stick)]
        )
        assert usb_mod.main() == 0
        assert not (stick / "track.m4a").exists()

    def test_runs_unprivileged_and_without_prompting(self, tmp_path, monkeypatch):
        """_no_format_env sets geteuid to 1000, isatty to False, and makes
        input()/confirm_wipe raise. Reaching any of them fails this test."""
        cfg, stick = _no_format_env(tmp_path, monkeypatch)
        monkeypatch.setattr(
            sys,
            "argv",
            ["prog", "--library", "car", "--no-format", "--dest", str(stick), "--execute"],
        )
        assert usb_mod.main() == 0
        assert (stick / "track.m4a").exists()

    def test_playlists_still_copied(self, tmp_path, monkeypatch):
        """--no-format runs copy_playlists exactly as the wipe path does.

        Uses --library alac deliberately: copy_playlists' rewriter resolves
        each entry against source_root.parent/"Playlists", which equals
        vault_root/Playlists only when the library is a direct child of the
        vault. ALAC-Library is; Libraries/CAR_Library (published there
        2026-09-03) is not, so car playlists are silently dropped. That is a
        pre-existing gap in copy_playlists, not something --no-format
        introduces, and is reported separately rather than fixed here."""
        cfg, stick = _no_format_env(tmp_path, monkeypatch)
        cfg.alac_library.mkdir(parents=True, exist_ok=True)
        (cfg.alac_library / "track.m4a").write_bytes(b"hello")
        playlists = cfg.vault_root / "Playlists"
        playlists.mkdir(parents=True, exist_ok=True)
        (playlists / "Drive.m3u8").write_text(
            "#EXTM3U\n#EXTINF:1,A - B\n../ALAC-Library/track.m4a\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            sys,
            "argv",
            ["prog", "--library", "alac", "--no-format", "--dest", str(stick), "--execute"],
        )
        assert usb_mod.main() == 0
        assert (stick / "Playlists" / "Drive.m3u8").exists()


class TestNoFormatTargetResolution:
    def test_missing_target_is_refused(self, tmp_path, monkeypatch):
        cfg, stick = _no_format_env(tmp_path, monkeypatch)
        monkeypatch.setattr(sys, "argv", ["prog", "--library", "car", "--no-format"])
        assert usb_mod.main() == 1

    def test_dest_that_is_not_a_directory_is_refused(self, tmp_path, monkeypatch):
        cfg, stick = _no_format_env(tmp_path, monkeypatch)
        monkeypatch.setattr(
            sys, "argv", ["prog", "--library", "car", "--no-format", "--dest", str(tmp_path / "nope")]
        )
        assert usb_mod.main() == 1

    def test_device_resolves_through_findmnt(self, tmp_path, monkeypatch, capsys):
        dev = _device(path="/dev/sdz")
        dev.partitions = ["/dev/sdz1"]
        cfg, stick = _no_format_env(tmp_path, monkeypatch, dev=dev)

        def _fake_run(cmd, **kwargs):
            if cmd[0] == "findmnt":
                return _FakeCompleted(stdout=f"{stick}\n")
            raise AssertionError(f"unexpected command: {cmd}")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        monkeypatch.setattr(
            sys, "argv", ["prog", "--library", "car", "--no-format", "--device", "/dev/sdz"]
        )
        assert usb_mod.main() == 0
        assert str(stick) in capsys.readouterr().out

    def test_device_with_no_mounted_partition_is_refused(self, tmp_path, monkeypatch):
        dev = _device(path="/dev/sdz")
        dev.partitions = ["/dev/sdz1"]
        cfg, stick = _no_format_env(tmp_path, monkeypatch, dev=dev)
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _FakeCompleted(returncode=1)
        )
        monkeypatch.setattr(
            sys, "argv", ["prog", "--library", "car", "--no-format", "--device", "/dev/sdz"]
        )
        assert usb_mod.main() == 1

    def test_device_with_two_mounted_partitions_refuses_to_guess(self):
        dev = _device(path="/dev/sdz")
        dev.partitions = ["/dev/sdz1", "/dev/sdz2"]
        import scripts.usb_transfer.transfer_to_usb as mod

        original = mod._findmnt_target
        try:
            mod._findmnt_target = lambda source: "/media/one" if source.endswith("1") else "/media/two"
            with pytest.raises(UsbTargetError, match="2 mounted partitions"):
                mounted_dir_for_device(dev)
        finally:
            mod._findmnt_target = original

    def test_denylisted_device_still_blocked_under_no_format(self, tmp_path, monkeypatch):
        """--no-format is exempt from the typed confirmation and the root
        check. It is NOT exempt from the denylist -- that gate is independent
        of both, and a --device that backs a critical mount is refused
        whatever else is on the command line."""
        dev = _device(path="/dev/sda")
        cfg, stick = _no_format_env(tmp_path, monkeypatch, dev=dev)
        monkeypatch.setattr(usb_mod, "critical_backing_disks", lambda *a, **k: {"/dev/sda"})
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(
            sys,
            "argv",
            ["prog", "--library", "car", "--no-format", "--device", "/dev/sda", "--execute"],
        )
        assert usb_mod.main() == 1
        assert calls == []


class TestNoFormatFreeSpace:
    def test_enough_space_passes(self, tmp_path):
        f = tmp_path / "track.m4a"
        f.write_bytes(b"x" * 16)
        assert check_free_space(tmp_path, [f]) is None

    def test_too_little_space_names_both_numbers(self, tmp_path, monkeypatch):
        f = tmp_path / "track.m4a"
        f.write_bytes(b"x" * 4096)
        monkeypatch.setattr(
            usb_mod.shutil, "disk_usage", lambda p: type("U", (), {"free": 100})()
        )
        with pytest.raises(UsbTargetError) as exc:
            check_free_space(tmp_path, [f])
        message = str(exc.value)
        assert "4,096 bytes" in message  # needed
        assert "100 bytes" in message  # available

    def test_main_refuses_before_copying_when_space_is_short(self, tmp_path, monkeypatch):
        cfg, stick = _no_format_env(tmp_path, monkeypatch)
        monkeypatch.setattr(
            usb_mod.shutil, "disk_usage", lambda p: type("U", (), {"free": 0})()
        )
        monkeypatch.setattr(
            sys,
            "argv",
            ["prog", "--library", "car", "--no-format", "--dest", str(stick), "--execute"],
        )
        assert usb_mod.main() == 1
        assert not (stick / "track.m4a").exists()


class TestDenylistAcrossEveryFlagCombination:
    """Gate 1 is independent of every other gate. Whatever combination of the
    new flags is passed, a device backing /, /home or the vault is refused and
    not one subprocess call is made."""

    @pytest.mark.parametrize(
        "extra",
        [
            [],
            ["--execute"],
            ["--filesystem", "fat32"],
            ["--filesystem", "exfat"],
            ["--filesystem", "fat32", "--execute"],
            ["--no-format"],
            ["--no-format", "--execute"],
        ],
    )
    @pytest.mark.parametrize("library", ["car", "alac"])
    def test_denylisted_device_refused(self, tmp_path, monkeypatch, library, extra):
        cfg = _cfg(tmp_path)
        for lib in (cfg.alac_library, cfg.car_library):
            lib.mkdir(parents=True, exist_ok=True)
            (lib / "track.m4a").write_bytes(b"x")
        monkeypatch.setattr(usb_mod, "get_config", lambda: cfg)
        monkeypatch.setattr(usb_mod, "list_removable_devices", lambda: [_device(path="/dev/sda")])
        monkeypatch.setattr(usb_mod, "critical_backing_disks", lambda *a, **k: {"/dev/sda"})
        monkeypatch.setattr(usb_mod, "check_mkfs_available", lambda filesystem: None)

        def _boom(*a, **k):
            raise AssertionError("a denylisted device reached a destructive gate")

        monkeypatch.setattr(usb_mod, "confirm_wipe", _boom)
        monkeypatch.setattr(usb_mod, "execute_commands", _boom)
        monkeypatch.setattr(
            sys, "argv", ["prog", "--library", library, "--device", "/dev/sda"] + extra
        )
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
        assert usb_mod.main() == 1
        assert calls == []


# ── Playlists that actually resolve (2026-09-05) ─────────────────────────────


class TestPlaylistEntriesResolve:
    """Both faults fixed here were invisible to string assertions and obvious
    the moment the emitted entries were resolved against the device tree.

    Measured on Grey's actual stick the same day: all 31,567 entries across
    its 60 playlists were dangling, pointing at ../ALAC_Archive/ which is not
    on the device at all."""

    def _stage(self, tmp_path, library):
        """A vault with one track in *library*, one playlist referencing it by
        the vault-relative path playlist.py writes, and the flat device tree
        copy_with_verification would have produced."""
        cfg = _cfg(tmp_path)
        cfg.ensure_dirs()
        source_root = usb_mod._source_dir(library, cfg)
        rel = Path("2026-08-17") / "Artist" / "Album" / "Song.m4a"
        track = source_root / rel
        track.parent.mkdir(parents=True, exist_ok=True)
        track.write_bytes(b"x")

        (cfg.vault_root / "Playlists").mkdir(parents=True, exist_ok=True)
        vault_relative = Path("..") / source_root.relative_to(cfg.vault_root) / rel
        (cfg.vault_root / "Playlists" / "Rock.m3u8").write_text(
            f"#EXTM3U\n#EXTINF:-1,Artist - Song\n{vault_relative.as_posix()}\n",
            encoding="utf-8",
        )

        dest = tmp_path / "device"
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_bytes(b"x")
        return cfg, source_root, dest

    @pytest.mark.parametrize("library", ["alac", "car"])
    def test_entries_resolve_on_the_device(self, tmp_path, library):
        cfg, source_root, dest = self._stage(tmp_path, library)
        written = copy_playlists(cfg.vault_root, source_root, dest)
        assert written == ["Rock.m3u8"], f"{library}: playlist was dropped entirely"
        assert _entries_resolve(dest / "Playlists" / "Rock.m3u8")

    def test_car_playlists_are_no_longer_dropped(self, tmp_path):
        """The car library lives at Libraries/CAR_Library, so source_root.parent
        is Libraries/, not the vault. Resolving entries against
        source_root.parent/"Playlists" therefore matched nothing and every car
        playlist was silently dropped -- the car edition shipped with none."""
        cfg, source_root, dest = self._stage(tmp_path, "car")
        assert source_root.parent != cfg.vault_root  # the condition that broke it
        assert copy_playlists(cfg.vault_root, source_root, dest) == ["Rock.m3u8"]

    def test_entry_climbs_out_of_the_playlists_directory(self, tmp_path):
        """The playlist is written to dest/Playlists/ but the audio lands at
        dest/<rel>, so each entry needs exactly one level of climb."""
        cfg, source_root, dest = self._stage(tmp_path, "alac")
        copy_playlists(cfg.vault_root, source_root, dest)
        entries = [
            ln
            for ln in (dest / "Playlists" / "Rock.m3u8").read_text().splitlines()
            if ln and not ln.startswith("#")
        ]
        assert entries == ["../2026-08-17/Artist/Album/Song.m4a"]

    def test_absolute_entry_outside_the_vault_still_filtered_out(self, tmp_path):
        """playlist.py falls back to an absolute path for a source outside
        vault_root. Joining an absolute path leaves it unchanged, so it must
        still be judged against source_root and dropped when it is elsewhere."""
        cfg, source_root, dest = self._stage(tmp_path, "alac")
        (cfg.vault_root / "Playlists" / "Odd.m3u8").write_text(
            "#EXTM3U\n#EXTINF:-1,Elsewhere - Track\n/somewhere/else/Track.m4a\n",
            encoding="utf-8",
        )
        written = copy_playlists(cfg.vault_root, source_root, dest)
        assert "Odd.m3u8" not in written

    def test_no_playlists_directory_writes_nothing_and_overwrites_nothing(self, tmp_path):
        """Grey's stick has a hand-made Playlists/ at its root. vault_root has
        no Playlists/ at all today, so copy_playlists must return early and
        leave what is already on the device alone."""
        cfg = _cfg(tmp_path)
        cfg.ensure_dirs()
        dest = tmp_path / "device"
        existing = dest / "Playlists" / "Mine.m3u8"
        existing.parent.mkdir(parents=True)
        existing.write_text("#EXTM3U\n", encoding="utf-8")

        assert not (cfg.vault_root / "Playlists").exists()
        assert copy_playlists(cfg.vault_root, cfg.alac_library, dest) == []
        assert existing.read_text() == "#EXTM3U\n"
