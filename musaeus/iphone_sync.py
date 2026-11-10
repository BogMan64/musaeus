"""`musaeus iphone-sync`: put the iPhone edition on the phone in one go.

Grey, 2026-11-09: the steps `scripts/iphone_transfer.py` prints, run by
MUSAEUS. The budget comes from the phone's real free space: with the printed
steps a worked example (35 GB) was taken as the size on a phone with 25 GB
free, and the copy could not have fit.

1. Check the tools and the phone; if it is not paired, ask Grey to tap Trust.
2. Read the phone's free space; mount VLC's Documents folder and measure it.
3. Ask before clearing VLC, every time (Grey's choice). Typed "yes":
   rebuild the iPhone edition to free + what VLC holds - HEADROOM, clear VLC,
   copy. Anything else: nothing on the phone is deleted and the edition is
   not rebuilt; the copy runs only if the songs not yet on the phone fit.
4. Nothing is cleared or copied unless the folder really is the phone's
   mount (a failed ifuse once let a copy fill a plain folder on the PC, review
   of #88, finding 13), and the phone is always unmounted at the end.
"""

from __future__ import annotations

import errno
import math
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from .edition_build import TMP_SUFFIX

APP_ID = "org.videolan.vlc-ios"
#: Left free on the phone: iOS needs room of its own, and sizes are estimates.
HEADROOM = 5_000_000_000
TOOLS = (
    ("idevice_id", "libimobiledevice-utils"),
    ("idevicepair", "libimobiledevice-utils"),
    ("ideviceinfo", "libimobiledevice-utils"),
    ("ifuse", "ifuse"),
    ("fusermount", "fuse"),
    ("rsync", "rsync"),
)


def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _attached() -> bool:
    r = _run(["idevice_id", "-l"])
    return r.returncode == 0 and bool(r.stdout.strip())


def _paired() -> bool:
    return _run(["idevicepair", "validate"]).returncode == 0


def _pair(ask: Callable[[str], str]) -> bool:
    _run(["idevicepair", "pair"])
    ask("  Unlock the phone and tap Trust, then press Enter here: ")
    _run(["idevicepair", "pair"])
    return _paired()


def _free_bytes() -> int | None:
    r = _run(["ideviceinfo", "-q", "com.apple.disk_usage", "-k", "AmountDataAvailable"])
    out = r.stdout.strip()
    return int(out) if r.returncode == 0 and out.isdigit() else None


def _mount(mount: Path) -> bool:
    """Mount VLC's Documents folder at *mount*; True only if it really is mounted."""
    try:
        mount.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        if e.errno not in (errno.EIO, errno.ENOTCONN):
            raise
        # A dead ifuse mount from an earlier run reads as an I/O error (11-09:
        # its unmount had failed, then the phone locked). Unmount and try again.
        _unmount(mount)
        try:
            mount.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"  {mount} is stuck ({e.strerror}). Run:  fusermount -u {mount}")
            return False
    if os.path.ismount(mount):
        print(f"  {mount} is already mounted. Unmount it first:  fusermount -u {mount}")
        return False
    if any(mount.iterdir()):
        print(f"  {mount} is a folder on this PC with files in it. Empty or move it first.")
        return False
    r = _run(["ifuse", "--documents", APP_ID, str(mount)])
    if r.returncode != 0:
        print(f"  Could not open VLC on the phone: {(r.stdout + r.stderr).strip()}")
        print("  Is VLC for iOS installed, and the phone unlocked?")
    return os.path.ismount(mount)


def _unmount(mount: Path) -> None:
    try:
        r = _run(["fusermount", "-u", str(mount)])
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  Could not run fusermount: {e}")
        return
    if r.returncode != 0 and os.path.ismount(mount):
        print(f"  Could not unmount {mount}: {(r.stdout + r.stderr).strip()}")
        print(f"  Close any window open on it, then run:  fusermount -u {mount}")


def _files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and not p.name.endswith(TMP_SUFFIX)]


def _size(root: Path) -> tuple[int, int]:
    files = _files(root)
    return len(files), sum(p.stat().st_size for p in files)


def _wipe(root: Path) -> None:
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _missing_bytes(src: Path, dest: Path) -> int:
    """Bytes of *src* songs not on the phone yet (missing, or a different size)."""
    total = 0
    for f in _files(src):
        size = f.stat().st_size
        there = dest / f.relative_to(src)
        if not (there.is_file() and there.stat().st_size == size):
            total += size
    return total


def _build(budget_gb: float) -> int:
    cmd = [
        sys.executable,
        "-m",
        "musaeus",
        "edition-build",
        "iphone",
        "--budget-gb",
        f"{budget_gb}",
    ]
    return subprocess.run(cmd).returncode


def _rsync(src: Path, mount: Path) -> int:
    cmd = ["rsync", "-a", "--info=progress2", f"--exclude=*{TMP_SUFFIX}", f"{src}/", f"{mount}/"]
    return subprocess.run(cmd).returncode


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"


def sync(src: Path, mount: Path, ask: Callable[[str], str] = input) -> int:
    """Run the whole sync; 0 when the songs are on the phone."""
    print("iPhone sync\n" + "=" * 62)
    missing = [f"{tool} (sudo apt install {pkg})" for tool, pkg in TOOLS if not shutil.which(tool)]
    if missing:
        print("  Missing: " + ", ".join(missing))
        return 1
    if not _attached():
        print("  No phone found. Plug it in with a data cable and unlock it.")
        return 1
    if not _paired() and not _pair(ask):
        print("  The phone is not paired. Unlock it, tap Trust, and run this again.")
        return 1
    free = _free_bytes()
    if free is None:
        print("  Could not read the phone's free space. Unlock it and run this again.")
        return 1

    if not _mount(mount):
        return 1
    try:
        count, held = _size(mount)
    finally:
        _unmount(mount)
    print(f"  Phone free space : {_gb(free)}")
    print(f"  VLC holds now    : {count:,} file(s), {_gb(held)}")
    answer = ask(
        f"\n  Delete everything in VLC on the phone first ({count:,} file(s), {_gb(held)})?\n"
        "  Type yes to delete; anything else only adds songs: "
    )
    wipe = answer.strip().lower() == "yes"

    if wipe:
        budget = free + held - HEADROOM
        if budget <= 0:
            print(f"  Not enough room: the phone needs more than {_gb(HEADROOM)} free.")
            return 1
        budget_gb = math.floor(budget / 1e7) / 100  # down, never up, to 0.01 GB
        print(f"\n  Building the iPhone edition to {budget_gb} GB ...")
        rc = _build(budget_gb)
        if rc != 0:
            print("  The edition build failed; nothing on the phone was changed.")
            return rc

    if not src.is_dir():
        print(f"  The iPhone edition does not exist yet at {src}.")
        return 1
    # A build can end over its budget (CodeRabbit on #310): never clear VLC for
    # a copy that cannot fit.
    if wipe and _size(src)[1] > free + held - HEADROOM:
        print(
            f"  The iPhone edition is {_gb(_size(src)[1])}; after clearing VLC the phone "
            f"would have {_gb(free + held)} with {_gb(HEADROOM)} kept spare. Nothing on the "
            "phone was changed."
        )
        return 1
    if not _mount(mount):
        return 1
    try:
        if wipe:
            print(f"  Clearing VLC ({count:,} file(s)) ...")
            _wipe(mount)
        else:
            need = _missing_bytes(src, mount)
            if need > free - HEADROOM:
                print(
                    f"  The songs not on the phone yet need {_gb(need)}; the phone has "
                    f"{_gb(free)} free and keeps {_gb(HEADROOM)} spare. Nothing was copied.\n"
                    "  Run again and type yes to clear VLC, or free space on the phone."
                )
                return 1
        print("  Copying ...")
        rc = _rsync(src, mount)
    finally:
        _unmount(mount)
    print("  Done: the songs are in VLC on the phone." if rc == 0 else f"  rsync stopped ({rc}).")
    return rc
