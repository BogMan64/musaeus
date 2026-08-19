#!/usr/bin/env python3
"""
MUSAEUS — USB Transfer (Phase 3, standalone tool, NOT a pipeline stage)

Wipes and reformats a USB drive to ExFAT (Android-compatible), then copies
the chosen library (Phase 2A's ALAC-Library or Phase 2B's AAC-Car output)
onto it with speed-monitored, checksum-verified transfer -- "quality over
speed," per Grey's own framing: prefer a slower, verified, thermally-sane
copy over a fast one that risks corruption or overheating a cheap USB
enclosure.

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
     (wipefs/parted/mkfs.exfat) -- os.geteuid() checked explicitly so an
     accidental unprivileged --execute fails cleanly instead of partially
     succeeding.
  5. --execute is required to do anything destructive; the default is a
     dry run that prints exactly what would happen.

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
"""

from __future__ import annotations

import argparse
import json
import os
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


def build_wipe_and_format_commands(device_path: str, label: str = "MUSAEUS") -> list[list[str]]:
    """Pure: returns the argv lists that would wipe+partition+format
    *device_path* to a single ExFAT partition. Does not execute anything --
    callers pass this to execute_commands() explicitly."""
    partition = f"{device_path}1" if not device_path[-1].isdigit() else f"{device_path}p1"
    return [
        ["wipefs", "--all", device_path],
        ["parted", "--script", device_path, "mklabel", "gpt"],
        ["parted", "--script", device_path, "mkpart", "primary", "0%", "100%"],
        ["mkfs.exfat", "-n", label, partition],
    ]


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
        # No canonical AAC-Car tier exists yet (2026-08-18) -- Phase 2B's
        # build_car_library.py writes to a RUNS-scoped staging/export area,
        # not a stable named location like ALAC-Library. This points at
        # wherever it last wrote; pass --source-dir explicitly if that's
        # wrong for your situation.
        return cfg.runs_root / "AAC-Car-Masked" / "_output"
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
# under *this* transfer's source_root, and rewritten to the flat relative
# form matching where those files actually land on the device.


def _rewrite_playlist_for_device(content: str, source_root: Path) -> str | None:
    """
    Given one playlist's raw .m3u8 text (as written by playlist.py --
    #EXTM3U header, then alternating #EXTINF / path-relative-to-vault_root
    lines), keep only the track pairs whose path resolves under
    source_root, rewritten relative to source_root (matching the flat
    layout copy_with_verification produces on the device). Returns None
    if no tracks in this playlist belong to source_root at all (nothing
    worth writing).
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
        # fallback) -- either way, resolve it relative to where playlist.py
        # actually wrote the .m3u8 file (vault_root/Playlists) to get the
        # real absolute path before checking whether it's under source_root.
        candidate = (source_root.parent / "Playlists" / path_line).resolve()
        try:
            rel = candidate.relative_to(source_root.resolve())
        except ValueError:
            continue  # not part of this transfer's library -- drop silently

        out_lines.append(extinf)
        out_lines.append(str(rel))
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
            rewritten = _rewrite_playlist_for_device(content, source_root)
            if rewritten is None:
                continue
            dest_playlists.mkdir(parents=True, exist_ok=True)
            (dest_playlists / m3u.name).write_text(rewritten, encoding="utf-8")
            written.append(m3u.name)
        except OSError as exc:
            print(f"  WARNING: could not copy playlist {m3u.name}: {exc}", file=sys.stderr)
    return written


# ── main ───────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MUSAEUS Phase 3 -- wipe + reformat USB to ExFAT, transfer a library"
    )
    parser.add_argument(
        "--list-devices", action="store_true", help="List removable devices and exit"
    )
    parser.add_argument("--library", choices=["alac", "car"], help="Which library to transfer")
    parser.add_argument("--source-dir", default=None, help="Override the source directory")
    parser.add_argument("--device", default=None, help="Target device, e.g. /dev/sdb")
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

    cfg = get_config()
    source_root = Path(args.source_dir) if args.source_dir else _source_dir(args.library, cfg)
    if not source_root.exists():
        print(f"ERROR: source not found: {source_root}", file=sys.stderr)
        return 1

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
    print(f"Target: {device.path} ({device.size_human})")

    if not args.execute:
        print("\nDRY RUN (pass --execute to actually wipe + transfer)")
        for cmd in build_wipe_and_format_commands(device.path):
            print(f"  would run: {' '.join(cmd)}")
        print(f"  would copy {len(files)} file(s) to a fresh ExFAT filesystem on {device.path}")
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

    execute_commands(build_wipe_and_format_commands(device.path), dry_run=False)

    partition = f"{device.path}1" if not device.path[-1].isdigit() else f"{device.path}p1"
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

    print(
        f"\n{len(result.ok)} copied+verified, {len(result.failed)} failed, "
        f"{result.cooldowns_triggered} cooldown(s) triggered"
    )
    for path, reason in result.failed:
        print(f"  FAILED {path}: {reason}")
    print(
        f"{len(playlists_written)} playlist(s) copied: {', '.join(playlists_written) or '(none)'}"
    )

    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
