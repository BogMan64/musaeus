#!/usr/bin/env python3
"""
MUSAEUS — USB Transfer (Phase 3, standalone tool, NOT a pipeline stage)

Wipes and reformats a USB drive, then copies the chosen library (Phase
2A's ALAC-Library or Phase 2B's published CAR_Library) onto it with
speed-monitored, checksum-verified transfer -- "quality over speed," per
Grey's own framing: prefer a slower, verified, thermally-sane copy over a
fast one that risks corruption or overheating a cheap USB enclosure.

It can also copy onto a filesystem that is already there (--no-format),
which is the safer everyday path and the only one that survives a head
unit having formatted the stick its own way.

THIS SCRIPT WIPES A PHYSICAL DEVICE. Read the safety design below in full
before running it against anything.

Safety design (confirmed with Grey 2026-08-18, before any of this was
written -- mirrors and extends scope doc §4.13's rule for DB resets, "no
AI tool call can trigger a destructive path non-interactively," applied
more strictly here since a physical wipe has no DB-snapshot rollback):

  1. Device enumeration and info display are read-only (lsblk/findmnt),
     safe to run any time.
  2. Denylist (independent of the confirmation step): resolves the real
     backing block device(s) of /, /home, and vault_root at run time via
     findmnt, and refuses to touch the whole-disk parent of any of them --
     not just an exact path match, so targeting the parent disk of a
     critical partition is caught too. This check runs even if the typed
     confirmation would otherwise pass; either gate alone can block the
     run, neither can be skipped by passing the other.
  3. Typed confirmation: the exact device path AND human-readable size
     must be typed back exactly as shown (not a fixed word like "WIPE"),
     forcing the operator to actually read the specific device info
     rather than reflexively confirming. Refuses outright with no prompt
     at all if stdin is not an interactive TTY -- same rule as
     cli.py's _cmd_reset(), applied here.
  4. Root required for the actual wipe/format subprocess calls
     (wipefs/parted/mkfs.*) -- os.geteuid() checked explicitly so an
     accidental unprivileged --execute fails cleanly instead of partially
     succeeding.
  5. --execute is required to do anything destructive; the default is a
     dry run that prints exactly what would happen.

--no-format (added 2026-09-05) skips gates 2 and 3 -- the typed
confirmation and the root check -- and ONLY those two, and only because
it destroys nothing: it emits no unmount, wipefs, parted or mkfs command
at all, and copies onto a filesystem that already exists. Gates 1, 4 and
5 still apply: a denylisted --device is still refused, and --execute is
still required before a single byte is written. This is deliberately not
implemented by threading dry_run=True through the format helpers -- that
would leave a real wipe one boolean away. The destructive branch is
simply never entered; tests/test_transfer_to_usb.py proves no such
command is emitted on that path.

Filesystem (added 2026-09-05, after a real failure): --filesystem
{exfat,fat32} defaults per --library --

    --library car   -> fat32 on an msdos (MBR) table
    --library alac  -> exfat on a gpt table

On 2026-09-04 Grey's Android head unit rejected a GPT+ExFAT stick as
"not set up for Android" and reformatted it itself, erasing the
transfer. FAT32-on-MBR is what the head unit chose for itself, so it is
what the car edition should have been written as in the first place.
ALAC keeps ExFAT: no ALAC master is going onto a FAT32 stick, and ExFAT
suits a ~490 GB library better. --filesystem overrides either default.

No test USB drive was available while this was built (2026-08-18) --
the wipe/format command construction (build_wipe_and_format_commands) is
covered by tests, but actual execution (execute_commands) is only ever
exercised in tests via a monkeypatched subprocess.run recorder, never a
real device. Manually verify against a real, deliberately-expendable test
drive before trusting this against anything with data you care about.

Speed monitoring ("quality over speed"): each file's write throughput is
compared against a rolling average of the last _SPEED_WINDOW files once
warmed up; a drop below _SPEED_DROP_THRESHOLD of that average triggers a
_COOLDOWN_SECONDS pause before continuing (possible sign of thermal
throttling or a failing connection on a cheap enclosure -- pausing lets
it recover rather than pushing through). Every copied file is re-hashed
(musaeus.hasher.file_hash, SHA-256) and compared against the source after
the copy -- a mismatch fails that file rather than trusting a fast but
unverified write. An I/O error retries once with a short backoff; a
second failure on the same file aborts the whole run rather than
continuing to write to a device that may be failing. These thresholds are
first-pass defaults, not measured against real hardware -- tune via the
CLI flags once tested against an actual device.

Usage:
    python3 scripts/usb_transfer/transfer_to_usb.py --list-devices
    python3 scripts/usb_transfer/transfer_to_usb.py --library alac        # dry run
    python3 scripts/usb_transfer/transfer_to_usb.py --library alac --device /dev/sdb --execute
    python3 scripts/usb_transfer/transfer_to_usb.py --library car --no-format --dest /media/grey/MUSAEUS --execute
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from musaeus.config import get_config  # noqa: E402
from musaeus.hasher import file_hash  # noqa: E402

_SPEED_WINDOW = 5  # files of history before speed-drop detection kicks in
_SPEED_DROP_THRESHOLD = 0.40  # current file's MB/s below 40% of rolling avg -> cooldown
_COOLDOWN_SECONDS = 15.0
_COPY_CHUNK_BYTES = 4 * 1024 * 1024  # 4 MiB
_MAX_RETRIES_PER_FILE = 1

_USB_LABEL = "MUSAEUS"

# Default filesystem per library -- see the module docstring for why the
# car edition is FAT32. --filesystem overrides either of these.
_DEFAULT_FILESYSTEM: dict[str, str] = {"car": "fat32", "alac": "exfat"}

# The partition table follows from the FILESYSTEM, not from --library.
# Keying it off the library would make "--library car --filesystem exfat"
# emit exfat-on-msdos, which is not a pairing anyone asked for; keying it
# off the filesystem means every combination of the two flags produces the
# pairing that filesystem is normally shipped with.
_PARTITION_TABLE: dict[str, str] = {"fat32": "msdos", "exfat": "gpt"}

# mkfs binary and the apt package providing it, per filesystem -- same
# (command, package) shape preflight.py uses for ffmpeg/fpcalc, so the
# missing-tool message can carry a real install hint.
_MKFS_FOR_FILESYSTEM: dict[str, tuple[str, str]] = {
    "exfat": ("mkfs.exfat", "exfatprogs"),
    "fat32": ("mkfs.vfat", "dosfstools"),
}

# FAT32 stores file size in 32 bits, so the per-file cap is one byte under
# 4 GiB -- a file of exactly 4 GiB is already too big, hence >= not >.
# Measured 2026-09-05: the largest file in either library is 611 MB, so
# this guard is defensive, not a live obstacle to the car edition.
_FAT32_MAX_FILE_BYTES = 4 * 1024**3

# FAT32 volume labels are 11 characters, uppercase (the 8.3-era limit
# mkfs.vfat -n still enforces).
_FAT32_MAX_LABEL_CHARS = 11


# ── Device enumeration (read-only) ───────────────────────────────────────────


@dataclass
class BlockDevice:
    path: str  # e.g. "/dev/sdb"
    size_bytes: int
    size_human: str
    model: str
    serial: str
    tran: str  # "usb", "sata", "nvme", ...
    removable: bool
    mountpoints: list[str]
    partitions: list[str] = field(default_factory=list)


def list_block_devices() -> list[BlockDevice]:
    """All whole-disk block devices via `lsblk -J`. Read-only."""
    proc = subprocess.run(
        [
            "lsblk",
            "-J",
            "-b",
            "-o",
            "NAME,PATH,SIZE,MODEL,SERIAL,TRAN,RM,TYPE,MOUNTPOINT",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"lsblk failed ({proc.returncode}): {proc.stderr[:300]}")

    data = json.loads(proc.stdout)
    devices: list[BlockDevice] = []
    for node in data.get("blockdevices", []):
        if node.get("type") != "disk":
            continue
        mountpoints: list[str] = []
        partitions: list[str] = []
        for child in node.get("children", []) or []:
            partitions.append(child.get("path", ""))
            mp = child.get("mountpoint")
            if mp:
                mountpoints.append(mp)
        if node.get("mountpoint"):
            mountpoints.append(node["mountpoint"])

        size_bytes = int(node.get("size") or 0)
        devices.append(
            BlockDevice(
                path=node.get("path", ""),
                size_bytes=size_bytes,
                size_human=_human_size(size_bytes),
                model=(node.get("model") or "").strip() or "unknown",
                serial=(node.get("serial") or "").strip() or "unknown",
                tran=node.get("tran") or "unknown",
                removable=bool(node.get("rm")),
                mountpoints=mountpoints,
                partitions=partitions,
            )
        )
    return devices


def list_removable_devices() -> list[BlockDevice]:
    return [d for d in list_block_devices() if d.removable]


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0:
            return f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}PB"


# ── Denylist (independent safety net) ────────────────────────────────────────


def _backing_disk_for_path(path: Path) -> str | None:
    """Resolve *path*'s mount source via findmnt, then walk lsblk's tree to
    find which whole-disk device it belongs to (a partition's parent disk,
    or the disk itself). Returns None if it can't be determined -- callers
    must treat that as "assume critical" (fail closed), never "assume safe"."""
    try:
        proc = subprocess.run(
            ["findmnt", "-no", "SOURCE", "--target", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return None
        source = proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not source or not source.startswith("/dev/"):
        return None

    try:
        devices = list_block_devices()
    except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError):
        return None

    for d in devices:
        if d.path == source or source in d.partitions:
            return d.path
    return None


class DenylistResolutionError(RuntimeError):
    """Raised when a critical path's backing device can't be determined.
    Callers must treat this as a hard refusal to proceed, never as
    'assume safe and continue' -- an incomplete denylist is worse than no
    answer, since it can silently miss a real critical device."""


def critical_backing_disks(vault_root: Path, extra_mounts: list[Path] | None = None) -> set[str]:
    """Whole-disk devices backing /, /home, vault_root, and any caller-
    supplied extra_mounts -- must never be wiped regardless of what the
    typed confirmation says. Fails closed: if ANY of these can't be
    resolved, raises rather than returning a partial set that a caller
    might mistake for 'nothing else is critical'.

    extra_mounts exists because "removable" (lsblk's RM flag, used to
    filter --list-devices/the interactive picker) is a heuristic, not a
    guarantee -- a long-attached backup drive in a USB enclosure can
    report RM=0 (excluding it from the picker) or RM=1 (a real risk this
    covers) depending on the controller/driver. Pass e.g.
    --extra-critical-mount /mnt/NUC8TB_BACKUP for any other real data
    drive that should never be a wipe target regardless of how it reports."""
    critical: set[str] = set()
    for p in (Path("/"), Path("/home"), vault_root, *(extra_mounts or [])):
        disk = _backing_disk_for_path(p)
        if disk is None:
            raise DenylistResolutionError(
                f"could not determine the backing device for {p} -- refusing to "
                f"compute a denylist without it, since an incomplete denylist "
                f"could silently miss a real critical device"
            )
        critical.add(disk)
    return critical


def is_denylisted(device_path: str, denylist: set[str]) -> bool:
    return device_path in denylist


# ── Typed confirmation ────────────────────────────────────────────────────────


def confirm_wipe(device: BlockDevice) -> bool:
    """Interactive typed-confirmation gate. Refuses immediately (no prompt)
    if stdin is not a TTY -- same discipline as cli.py's _cmd_reset()."""
    print("\n  MUSAEUS — Phase 3 USB Wipe + Transfer")
    print(f"  Device:       {device.path}")
    print(f"  Size:         {device.size_human}")
    print(f"  Model:        {device.model}")
    print(f"  Serial:       {device.serial}")
    print(f"  Transport:    {device.tran}")
    print(f"  Mountpoints:  {device.mountpoints or '(none)'}")
    print("\n  THIS WILL PERMANENTLY ERASE EVERYTHING ON THIS DEVICE.")

    if not sys.stdin.isatty():
        print("  ⚠  No TTY detected — refusing to wipe non-interactively.")
        return False

    expected = f"{device.path} {device.size_human}"
    try:
        typed = input(f'  Type "{expected}" exactly to confirm wipe: ').strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.")
        return False

    if typed != expected:
        print("  Confirmation text did not match exactly — cancelled.")
        return False
    return True


# ── Wipe + format (command construction is pure; execution is separate) ─────


def build_unmount_commands(device: BlockDevice) -> list[list[str]]:
    """Pure: unmount commands for every currently-mounted partition of
    *device*. wipefs refuses to touch a busy/mounted device -- hit for
    real 2026-09-03: /dev/sde was still auto-mounted at /media/grey/MyTunes
    from being plugged in, and wipefs failed with "Device or resource
    busy" instead of unmounting it first."""
    return [["umount", mp] for mp in device.mountpoints]


class FormatToolingError(RuntimeError):
    """Raised when the mkfs binary for the requested filesystem is missing.
    Checked before anything destructive runs -- discovering it after wipefs
    and parted have already gone through leaves an erased, unformatted
    stick and nothing to roll back to."""


def partition_path_for(device_path: str) -> str:
    """The first partition's device node for *device_path*. NVMe-style
    names already end in a digit and take a "p" separator (nvme0n1 ->
    nvme0n1p1); plain ones do not (sdb -> sdb1)."""
    return f"{device_path}1" if not device_path[-1].isdigit() else f"{device_path}p1"


def validate_label(label: str, filesystem: str) -> None:
    """Pure. Raises ValueError if *label* is unusable on *filesystem*. Only
    FAT32 constrains it. Validated up front because mkfs is the LAST of the
    four commands -- by the time it rejects a label, wipefs and both parted
    calls have already run, and the stick is erased and repartitioned with
    nothing to roll back to.

    Both rules measured against mkfs.fat 4.2 on 2026-09-05, not assumed:

      - 12 characters -> "Label can be no longer than 11 characters",
        exit 1, no filesystem written. A hard failure; this guard is what
        turns it into a refusal before the wipe instead of after it.
      - lower case -> exit 0 with only "Warning: lowercase labels might
        not work properly on some systems", and the label written as
        given. So this rule is deliberately STRICTER than mkfs.vfat: the
        target is a picky Android head unit that has already rejected one
        stick, and "might not work properly on some systems" is not a risk
        worth taking for a cosmetic label."""
    if filesystem != "fat32":
        return
    if len(label) > _FAT32_MAX_LABEL_CHARS:
        raise ValueError(
            f"FAT32 volume label {label!r} is {len(label)} characters -- mkfs.vfat "
            f"refuses anything over {_FAT32_MAX_LABEL_CHARS}, and would only do so "
            f"after the wipe and repartition had already happened."
        )
    if label != label.upper():
        raise ValueError(
            f"FAT32 volume label {label!r} must be uppercase. mkfs.vfat only warns "
            f"(\"lowercase labels might not work properly on some systems\") and "
            f"writes it anyway -- refused here because the target is a head unit. "
            f"Use {label.upper()!r}."
        )


def _which_including_sbin(command: str) -> str | None:
    """shutil.which, extended with the sbin directories.

    Measured 2026-09-05, and the reason this helper exists: mkfs.vfat and
    mkfs.exfat are both installed on this machine and both live in
    /usr/sbin, which Debian leaves off a non-root user's PATH. A plain
    shutil.which() therefore reports "not found" for a tool that is
    installed and will run fine -- and the dry run is run unprivileged
    (console.py only adds sudo for --execute), so the tooling check would
    have told Grey to apt-install a package he already had, on exactly the
    car transfer this change exists to enable. Look where the command will
    actually be found from, not where this process happens to look."""
    return shutil.which(command) or shutil.which(
        command,
        path=os.pathsep.join(
            [os.environ.get("PATH", ""), "/usr/local/sbin", "/usr/sbin", "/sbin"]
        ),
    )


def check_mkfs_available(filesystem: str) -> None:
    """Raises FormatToolingError with an install hint if the mkfs binary for
    *filesystem* is missing -- same (command, apt package) shape preflight.py
    uses for ffmpeg. dosfstools in particular is not universally installed,
    and finding that out mid-format is finding it out too late: wipefs and
    parted have already run by the time mkfs is reached."""
    command, apt_package = _MKFS_FOR_FILESYSTEM[filesystem]
    if _which_including_sbin(command) is None:
        raise FormatToolingError(
            f"{command} not found -- required to format {filesystem}. "
            f"Install: sudo apt install {apt_package}"
        )


def files_too_big_for_fat32(files: list[Path]) -> list[tuple[Path, int]]:
    """Pure-ish (stat only): every file in *files* at or above FAT32's
    per-file cap, as (path, size) pairs. Callers must run this BEFORE
    emitting any destructive command -- a file that will not fit is a
    reason not to format FAT32 at all, and discovering it during the copy
    means discovering it on an already-erased stick.

    Measured 2026-09-05: the largest file across both libraries is 611 MB,
    so this is expected to return empty. It is a guard against a future
    library, not a current obstacle."""
    oversized: list[tuple[Path, int]] = []
    for f in files:
        try:
            size = f.stat().st_size
        except OSError:
            continue  # unreadable now will fail loudly in the copy anyway
        if size >= _FAT32_MAX_FILE_BYTES:
            oversized.append((f, size))
    return oversized


def build_wipe_and_format_commands(
    device_path: str, label: str = _USB_LABEL, filesystem: str = "exfat"
) -> list[list[str]]:
    """Pure: returns the argv lists that would wipe+partition+format
    *device_path* to a single *filesystem* partition. Does not execute
    anything -- callers pass this to execute_commands() explicitly.

    The partition table comes from _PARTITION_TABLE[filesystem]: fat32 ->
    msdos, exfat -> gpt. FAT32 additionally passes "fat32" as parted's
    fs-type argument, which is not cosmetic: on an msdos table that sets
    the partition type byte to 0x0c (W95 FAT32 LBA). Without it parted
    leaves 0x83 (Linux), and a head unit that reads the partition table
    before the filesystem can reject the stick on that alone -- which is
    the exact class of failure this filesystem choice exists to fix."""
    validate_label(label, filesystem)
    table = _PARTITION_TABLE[filesystem]
    partition = partition_path_for(device_path)

    if filesystem == "fat32":
        mkpart = ["parted", "--script", device_path, "mkpart", "primary", "fat32", "0%", "100%"]
        mkfs = ["mkfs.vfat", "-F", "32", "-n", label, partition]
    else:
        mkpart = ["parted", "--script", device_path, "mkpart", "primary", "0%", "100%"]
        mkfs = ["mkfs.exfat", "-n", label, partition]

    return [
        ["wipefs", "--all", device_path],
        ["parted", "--script", device_path, "mklabel", table],
        mkpart,
        mkfs,
    ]


def wait_for_partition(partition_path: str, timeout: float = 10.0, poll_interval: float = 0.5) -> None:
    """parted's partition-table change is picked up by the kernel
    immediately, but the /dev/sdXN device node is created asynchronously
    by udev -- calling mkfs.exfat right after parted returns can race
    that. Hit for real 2026-09-03: mkfs.exfat failed with "open failed :
    /dev/sde1, No such file or directory" even though the node existed
    moments later. Poll for it instead of assuming it's already there."""
    deadline = time.monotonic() + timeout
    while not Path(partition_path).exists():
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"partition {partition_path} did not appear within {timeout}s "
                "of partitioning -- kernel/udev may not have caught up"
            )
        time.sleep(poll_interval)


def execute_commands(commands: list[list[str]], dry_run: bool) -> None:
    if dry_run:
        for cmd in commands:
            print(f"  [DRY RUN] would run: {' '.join(cmd)}")
        return

    if os.geteuid() != 0:
        raise PermissionError("root required to wipe/format a block device (run with sudo)")

    for cmd in commands:
        print(f"  running: {' '.join(cmd)}")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"{cmd[0]} failed ({proc.returncode}): {proc.stderr[:500]}")


# ── --no-format target resolution (destroys nothing) ─────────────────────────


class UsbTargetError(RuntimeError):
    """Raised when a --no-format run cannot work out where to copy, or when
    the destination has not got room. Nothing destructive has happened by
    the time this is raised -- it is a refusal to start, not a partial
    result."""


def _findmnt_target(source: str) -> str | None:
    """The directory *source* (a device node) is currently mounted at, or
    None. Read-only, same tool the denylist uses."""
    try:
        proc = subprocess.run(
            ["findmnt", "-no", "TARGET", source],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    target = proc.stdout.strip().splitlines()
    return target[0].strip() if target and target[0].strip() else None


def mounted_dir_for_device(device: BlockDevice) -> Path:
    """Where *device*'s mounted partition currently lives, for --no-format.

    Resolved live via findmnt rather than trusting the mountpoints lsblk
    reported when the device list was built, and deliberately refuses to
    guess: zero mounted partitions and more than one are both errors,
    because picking the wrong one silently writes a library onto the wrong
    filesystem. --dest exists precisely so the caller can say which."""
    candidates = list(device.partitions) or [device.path]
    mounted = [(part, t) for part in candidates if (t := _findmnt_target(part))]

    if not mounted:
        raise UsbTargetError(
            f"{device.path} has no mounted partition -- --no-format copies onto a "
            f"filesystem that is already there, so mount it first (or pass --dest "
            f"with the directory it is mounted at)."
        )
    if len(mounted) > 1:
        listed = ", ".join(f"{part} at {t}" for part, t in mounted)
        raise UsbTargetError(
            f"{device.path} has {len(mounted)} mounted partitions ({listed}) -- "
            f"refusing to guess which one you meant. Pass --dest with the one to "
            f"copy onto."
        )
    return Path(mounted[0][1])


def check_free_space(dest_root: Path, files: list[Path]) -> None:
    """Raises UsbTargetError if *files* will not fit under *dest_root*.

    Deliberately conservative: it counts every source byte, including files
    already present on the device that the copy would overwrite rather than
    add. That can refuse a re-copy that would in fact have fitted -- the
    trade is on purpose, since the alternative is filling a device mid-copy
    and leaving a half-written library that looks complete."""
    needed = sum(f.stat().st_size for f in files)
    free = shutil.disk_usage(dest_root).free
    if needed > free:
        raise UsbTargetError(
            f"not enough free space on {dest_root}: need {_human_size(needed)} "
            f"({needed:,} bytes), have {_human_size(free)} ({free:,} bytes) free. "
            f"This counts every source file, including any already on the device "
            f"that would be overwritten."
        )


# ── Speed-monitored, verified copy ───────────────────────────────────────────


@dataclass
class CopyResult:
    ok: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)
    cooldowns_triggered: int = 0


def _copy_one(src: Path, dst: Path) -> float:
    """Copy src -> dst in chunks. Returns elapsed seconds."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    with open(src, "rb") as rf, open(dst, "wb") as wf:
        while chunk := rf.read(_COPY_CHUNK_BYTES):
            wf.write(chunk)
    return time.monotonic() - start


def copy_with_verification(
    files: list[Path],
    source_root: Path,
    dest_root: Path,
    cooldown_seconds: float = _COOLDOWN_SECONDS,
    speed_window: int = _SPEED_WINDOW,
    speed_drop_threshold: float = _SPEED_DROP_THRESHOLD,
) -> CopyResult:
    """
    Copy every file in *files* (paths under source_root) to the same
    relative location under dest_root. "Quality over speed": every file is
    re-hashed and compared against its source after copy (mismatch = failed,
    not silently trusted); a throughput drop vs. the recent rolling average
    triggers a cooldown pause rather than pushing through at full speed. An
    I/O error retries once; a second failure on the same file aborts the
    whole run (returns immediately) rather than continuing to write to a
    device that may be failing.
    """
    result = CopyResult()
    recent_speeds: list[float] = []

    for src in files:
        rel = src.relative_to(source_root)
        dst = dest_root / rel

        attempt = 0
        while True:
            try:
                elapsed = _copy_one(src, dst)
                break
            except OSError as exc:
                attempt += 1
                if attempt > _MAX_RETRIES_PER_FILE:
                    result.failed.append((str(src), f"I/O error after retry: {exc}"))
                    return result  # abort whole run -- device may be failing
                time.sleep(2.0)

        size_mb = src.stat().st_size / (1024 * 1024)
        mbps = size_mb / elapsed if elapsed > 0 else float("inf")

        if len(recent_speeds) >= speed_window:
            avg = sum(recent_speeds[-speed_window:]) / speed_window
            if avg > 0 and mbps < avg * speed_drop_threshold:
                print(
                    f"  ⚠ speed drop: {mbps:.1f} MB/s vs. rolling avg {avg:.1f} MB/s "
                    f"— cooling down {cooldown_seconds:.0f}s"
                )
                result.cooldowns_triggered += 1
                time.sleep(cooldown_seconds)
        recent_speeds.append(mbps)

        if file_hash(src) != file_hash(dst):
            result.failed.append((str(src), "post-copy hash mismatch"))
            continue

        result.ok.append(str(src))

    return result


# ── Source resolution ─────────────────────────────────────────────────────────


def _source_dir(library: str, cfg) -> Path:
    if library == "alac":
        return cfg.alac_library
    if library == "car":
        # Was cfg.runs_root / "AAC-Car-Masked" / "_output" -- the
        # build/staging area, not the finished edition -- because no
        # canonical Car Edition tier existed yet when this was written
        # (2026-08-18). That changed 2026-09-03: the built-and-masked
        # edition now gets PUBLISHED to cfg.car_library
        # (Libraries/CAR_Library), the same stable named location
        # ALAC-Library already was for the lossless edition.
        #
        # Pointing this at the old staging path today would silently
        # transfer the wrong audio: RUNS/AAC-Car-Masked/_output/encoded/
        # holds the PRE-MASKING intermediate (no car-cabin noise mixed
        # in), left in place deliberately so a re-run can skip
        # already-encoded files rather than a finished product -- and
        # _output/masked/ itself is mostly empty now, its BATCH_001
        # contents already moved out to cfg.car_library, leaving only a
        # handful of noise-bed files the publish step does not touch.
        return cfg.car_library
    raise ValueError(f"unknown library: {library}")


# ── Playlists ─────────────────────────────────────────────────────────────────
#
# 2026-08-19 fix: this script never copied vault_root/Playlists/ onto the
# USB at all -- confirmed gap (Claude(chat)'s review). A naive copy
# wouldn't have worked anyway: playlist.py's playlists mix ALAC-Library
# and AAC-Car entries in the same file (per-track, whichever of
# car_export_path/file_path was set) and use paths relative to
# vault_root (e.g. "../ALAC-Library/2026-08-17/Artist/Album/Track.m4a"),
# but this script's own copy is flat -- files land directly under the
# device root, not under a same-named ALAC-Library/AAC-Car subfolder. So
# each playlist is filtered down to only the entries that actually exist
# under *this* transfer's source_root, and rewritten to the relative form
# matching where those files actually land on the device.
#
# 2026-09-05: that rewrite emitted playlists whose entries did not
# resolve. Two independent path faults, both found by resolving the
# emitted entries instead of reading the emitted strings -- the tests
# asserted the text and so agreed with the bug:
#
#   1. Entries were resolved against source_root.parent/"Playlists",
#      which equals vault_root/"Playlists" only when the library is a
#      direct child of the vault. ALAC-Library is; Libraries/CAR_Library
#      (published there 2026-09-03) is not, so EVERY car playlist was
#      dropped as "not part of this transfer" and the car edition
#      silently shipped no playlists at all.
#   2. Entries were written relative to source_root, but the playlist
#      file itself is written to dest_root/"Playlists"/. A player
#      resolves them from the playlist's own directory, so they pointed
#      at dest_root/Playlists/Artist/... -- one level too deep, for every
#      entry, on both libraries.


def _rewrite_playlist_for_device(
    content: str, source_root: Path, vault_root: Path
) -> str | None:
    """
    Given one playlist's raw .m3u8 text (as written by playlist.py --
    #EXTM3U header, then alternating #EXTINF / path-relative-to-vault_root
    lines), keep only the track pairs whose path resolves under
    source_root, rewritten to where those files actually land on the
    device. Returns None if no tracks in this playlist belong to
    source_root at all (nothing worth writing).

    *vault_root* is passed in rather than derived from source_root: the
    entries are relative to vault_root/"Playlists" because that is where
    playlist.py wrote them, and source_root.parent only happens to equal
    vault_root for libraries sitting directly under it.
    """
    lines = content.splitlines()
    out_lines = ["#EXTM3U"]
    kept = 0

    i = 1  # skip #EXTM3U
    while i < len(lines):
        if not lines[i].startswith("#EXTINF"):
            i += 1
            continue
        extinf = lines[i]
        path_line = lines[i + 1] if i + 1 < len(lines) else ""
        i += 2

        # path_line is relative to vault_root/Playlists (e.g.
        # "../ALAC-Library/2026-08-17/Artist/Album/Track.m4a") or, for a
        # source outside vault_root, an absolute path (playlist.py's own
        # fallback -- joining an absolute path here yields it unchanged).
        # Either way, resolve it against where playlist.py actually wrote
        # the .m3u8 (vault_root/Playlists) to get the real absolute path
        # before checking whether it's under source_root.
        candidate = (vault_root / "Playlists" / path_line).resolve()
        try:
            rel = candidate.relative_to(source_root.resolve())
        except ValueError:
            continue  # not part of this transfer's library -- drop silently

        # The audio lands at dest_root/<rel>, but this playlist lands at
        # dest_root/Playlists/<name>.m3u8, and a player resolves entries
        # from the playlist's own directory -- so the entry has to climb
        # out of Playlists/ first. Forward slashes: the target is a FAT32
        # stick read by a car head unit, not a local OS path.
        out_lines.append(extinf)
        out_lines.append(f"../{rel.as_posix()}")
        kept += 1

    if not kept:
        return None
    return "\n".join(out_lines) + "\n"


def copy_playlists(vault_root: Path, source_root: Path, dest_root: Path) -> list[str]:
    """Filter+rewrite every playlist under vault_root/Playlists/ for this
    transfer's source_root, writing the results to dest_root/Playlists/.
    Returns the list of playlist filenames actually written (skips ones
    with no matching tracks). Best-effort: a single playlist's failure to
    read/write is logged and skipped, never aborts the whole transfer --
    the audio library itself is already safely on the device by the time
    this runs."""
    playlist_dir = vault_root / "Playlists"
    if not playlist_dir.exists():
        return []

    written: list[str] = []
    dest_playlists = dest_root / "Playlists"
    for m3u in sorted(playlist_dir.glob("*.m3u8")):
        try:
            content = m3u.read_text(encoding="utf-8")
            rewritten = _rewrite_playlist_for_device(content, source_root, vault_root)
            if rewritten is None:
                continue
            dest_playlists.mkdir(parents=True, exist_ok=True)
            (dest_playlists / m3u.name).write_text(rewritten, encoding="utf-8")
            written.append(m3u.name)
        except OSError as exc:
            print(f"  WARNING: could not copy playlist {m3u.name}: {exc}", file=sys.stderr)
    return written


# ── main ───────────────────────────────────────────────────────────────────────


def _report(result: CopyResult, playlists_written: list[str]) -> None:
    """The end-of-run summary, shared by the wipe path and --no-format so
    the two cannot drift into reporting the same outcome differently."""
    print(
        f"\n{len(result.ok)} copied+verified, {len(result.failed)} failed, "
        f"{result.cooldowns_triggered} cooldown(s) triggered"
    )
    for path, reason in result.failed:
        print(f"  FAILED {path}: {reason}")
    print(
        f"{len(playlists_written)} playlist(s) copied: {', '.join(playlists_written) or '(none)'}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "MUSAEUS Phase 3 -- transfer a library to USB, either onto a freshly "
            "wiped+reformatted device or (--no-format) onto the filesystem already there"
        )
    )
    parser.add_argument(
        "--list-devices", action="store_true", help="List removable devices and exit"
    )
    parser.add_argument("--library", choices=["alac", "car"], help="Which library to transfer")
    parser.add_argument("--source-dir", default=None, help="Override the source directory")
    parser.add_argument("--device", default=None, help="Target device, e.g. /dev/sdb")
    parser.add_argument(
        "--dest",
        default=None,
        metavar="DIR",
        help=(
            "Directory to copy into, for --no-format -- e.g. /media/grey/MUSAEUS. "
            "Preferred over --device on that path: it says exactly which mounted "
            "filesystem to write to instead of guessing from the partition table."
        ),
    )
    parser.add_argument(
        "--no-format",
        action="store_true",
        help=(
            "Copy onto the existing filesystem without wiping, partitioning or "
            "formatting anything. Needs --dest (or --device with exactly one "
            "mounted partition). Destroys nothing, so it needs neither root nor "
            "the typed wipe confirmation -- but still needs --execute."
        ),
    )
    parser.add_argument(
        "--filesystem",
        choices=["exfat", "fat32"],
        default=None,
        help=(
            "Filesystem to format to. Default depends on --library: car -> fat32 "
            "(on an msdos/MBR table, what the Android head unit chose for itself), "
            "alac -> exfat (on gpt). Ignored with --no-format."
        ),
    )
    parser.add_argument(
        "--execute", action="store_true", help="Actually wipe + copy (default: dry run)"
    )
    parser.add_argument("--cooldown-seconds", type=float, default=_COOLDOWN_SECONDS)
    parser.add_argument(
        "--extra-critical-mount",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "Additional mount point whose backing disk must never be a wipe "
            "target, beyond /, /home, and the vault (repeatable). lsblk's "
            "removable flag is a heuristic, not a guarantee -- use this for "
            "any other real data drive (e.g. a long-attached USB backup "
            "enclosure) that should always be protected regardless of how "
            "it reports."
        ),
    )
    args = parser.parse_args()
    extra_mounts = [Path(p) for p in args.extra_critical_mount]

    if args.list_devices:
        for d in list_removable_devices():
            print(f"  {d.path}  {d.size_human}  {d.model}  serial={d.serial}  tran={d.tran}")
        return 0

    if not args.library:
        parser.error("--library {alac,car} is required (or use --list-devices)")

    # argparse default is None, not a filesystem, so an explicit --filesystem
    # stays distinguishable from a defaulted one -- that is what makes
    # "--library car --filesystem exfat" an override rather than a no-op.
    filesystem = args.filesystem or _DEFAULT_FILESYSTEM[args.library]
    if args.no_format and args.filesystem:
        print(
            f"  note: --filesystem {args.filesystem} ignored -- --no-format copies "
            f"onto whatever filesystem is already on the target.",
            file=sys.stderr,
        )

    cfg = get_config()
    source_root = Path(args.source_dir) if args.source_dir else _source_dir(args.library, cfg)
    if not source_root.exists():
        print(f"ERROR: source not found: {source_root}", file=sys.stderr)
        return 1

    # --dest is a plain directory: no device to look up, and nothing to
    # denylist, because copying files into a mounted directory destroys
    # nothing the way a whole-disk wipe does. --device still resolves to a
    # BlockDevice on both paths, so the denylist below covers both.
    device: BlockDevice | None = None
    dest_root: Path | None = None

    if args.no_format and args.dest:
        dest_root = Path(args.dest)
        if not dest_root.is_dir():
            print(f"ERROR: --dest is not a directory: {dest_root}", file=sys.stderr)
            return 1
    elif args.no_format and not args.device:
        print(
            "ERROR: --no-format needs a target: --dest DIR (preferred) or --device "
            "with exactly one mounted partition.",
            file=sys.stderr,
        )
        return 1
    else:
        devices = list_removable_devices()
        device = next((d for d in devices if d.path == args.device), None) if args.device else None
        if args.device and device is None:
            print(f"ERROR: {args.device} not found among removable devices", file=sys.stderr)
            for d in devices:
                print(f"  available: {d.path}  {d.size_human}  {d.model}")
            return 1
        if device is None:
            print("Musaeus needs a USB location. Removable devices found:")
            for d in devices:
                print(f"  {d.path}  {d.size_human}  {d.model}  serial={d.serial}")
            if not sys.stdin.isatty():
                print("ERROR: no --device given and no TTY to prompt for one.", file=sys.stderr)
                return 1
            try:
                chosen = input("Device path to use: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nCancelled.")
                return 1
            device = next((d for d in devices if d.path == chosen), None)
            if device is None:
                print(f"ERROR: {chosen} not found among removable devices", file=sys.stderr)
                return 1

    # Gate 1 applies to any run that named a device, --no-format included:
    # it is independent of the confirmation, and --no-format only earns a
    # pass on the confirmation and the root check.
    if device is not None:
        try:
            denylist = critical_backing_disks(cfg.vault_root, extra_mounts)
        except DenylistResolutionError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        if is_denylisted(device.path, denylist):
            print(
                f"ERROR: {device.path} backs a critical mount (/, /home, or the vault) — refusing.",
                file=sys.stderr,
            )
            return 1

    files = sorted(p for p in source_root.rglob("*") if p.is_file())
    print(f"Source: {source_root} ({len(files)} file(s))")

    # ── --no-format: copy onto what is already there ─────────────────────────
    #
    # This branch returns unconditionally. It cannot fall through to the
    # wipe path below, which is the whole safety argument for skipping the
    # typed confirmation and the root check -- there is no boolean that
    # turns this into a destructive run.
    if args.no_format:
        if dest_root is None:
            assert device is not None  # the target resolution above guarantees one
            try:
                dest_root = mounted_dir_for_device(device)
            except UsbTargetError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1

        print(f"Target: {dest_root} (existing filesystem — nothing will be formatted)")
        try:
            check_free_space(dest_root, files)
        except (UsbTargetError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        if not args.execute:
            print("\nDRY RUN (pass --execute to actually transfer)")
            print("  no unmount, wipefs, parted or mkfs command — nothing is formatted")
            print(f"  would copy {len(files)} file(s) onto the existing filesystem at {dest_root}")
            return 0

        result = copy_with_verification(
            files, source_root, dest_root, cooldown_seconds=args.cooldown_seconds
        )
        playlists_written = copy_playlists(cfg.vault_root, source_root, dest_root)
        _report(result, playlists_written)
        return 1 if result.failed else 0

    # ── Wipe + format path ───────────────────────────────────────────────────
    assert device is not None  # every non---no-format route above resolves one
    table = _PARTITION_TABLE[filesystem]
    print(f"Target: {device.path} ({device.size_human}) — {filesystem} on a {table} table")

    # Both of these run BEFORE the dry-run/execute branch, so a dry run
    # surfaces them too. Checking after --execute would mean discovering a
    # missing mkfs, or a file that cannot fit, on an already-erased stick.
    try:
        check_mkfs_available(filesystem)
    except FormatToolingError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if filesystem == "fat32":
        oversized = files_too_big_for_fat32(files)
        if oversized:
            print(
                f"ERROR: {len(oversized)} file(s) are at or above FAT32's "
                f"{_human_size(_FAT32_MAX_FILE_BYTES)} per-file limit — refusing to format "
                f"FAT32. Use --filesystem exfat, or leave those files out.",
                file=sys.stderr,
            )
            for path, size in oversized[:5]:
                print(f"  {_human_size(size)}  {path}", file=sys.stderr)
            if len(oversized) > 5:
                print(f"  ... and {len(oversized) - 5} more", file=sys.stderr)
            return 1

    try:
        format_commands = build_wipe_and_format_commands(device.path, filesystem=filesystem)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not args.execute:
        print("\nDRY RUN (pass --execute to actually wipe + transfer)")
        for cmd in build_unmount_commands(device) + format_commands:
            print(f"  would run: {' '.join(cmd)}")
        print(
            f"  would copy {len(files)} file(s) to a fresh {filesystem} filesystem "
            f"on {device.path}"
        )
        return 0

    if not confirm_wipe(device):
        return 1
    try:
        # Re-checked post-confirmation -- mount state can change between the
        # initial check and the typed confirmation completing.
        recheck_denylist = critical_backing_disks(cfg.vault_root, extra_mounts)
    except DenylistResolutionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if is_denylisted(device.path, recheck_denylist):
        print(f"ERROR: {device.path} became denylisted — refusing.", file=sys.stderr)
        return 1

    # Mount state can also change between the initial listing and now, same
    # reasoning as the denylist recheck above -- re-fetch it fresh rather
    # than trusting the mountpoints captured before the typed confirmation.
    refreshed = next((d for d in list_removable_devices() if d.path == device.path), device)
    execute_commands(build_unmount_commands(refreshed), dry_run=False)

    partition = partition_path_for(device.path)
    # The SAME list the dry run printed above, not a second call with the
    # same arguments -- two call sites is how a dry run starts lying about
    # what --execute will actually do.
    wipe_and_format = format_commands
    # wipefs + both parted calls, then mkfs.exfat separately -- the wait in
    # between is why this isn't one execute_commands() call. See
    # wait_for_partition's docstring.
    execute_commands(wipe_and_format[:-1], dry_run=False)
    wait_for_partition(partition)
    execute_commands(wipe_and_format[-1:], dry_run=False)

    mount_point = Path(f"/mnt/musaeus_usb_{int(time.time())}")
    mount_point.mkdir(parents=True, exist_ok=True)
    subprocess.run(["mount", partition, str(mount_point)], check=True)

    try:
        result = copy_with_verification(
            files, source_root, mount_point, cooldown_seconds=args.cooldown_seconds
        )
        playlists_written = copy_playlists(cfg.vault_root, source_root, mount_point)
    finally:
        subprocess.run(["umount", str(mount_point)], check=False)

    _report(result, playlists_written)
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
