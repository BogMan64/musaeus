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
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from .edition_build import TMP_SUFFIX
from .editions import primary_genre
from .stages.playlist import _safe_genre

APP_ID = "org.videolan.vlc-ios"
#: Left free on the phone: iOS needs room of its own, and sizes are estimates.
HEADROOM = 5_000_000_000
#: rsync exit codes for a link that broke mid-copy (file I/O, stream, partial,
#: timeout): worth one more try. Never 20 -- that is Ctrl-C.
LINK_LOST = frozenset({11, 12, 23, 30})
#: Tries after the first copy when the phone drops the link (it did twice on
#: 2026-11-09/10: a pulled cable, then the phone closed it when used).
RECONNECTS = 3
RECONNECT_WAIT = 10  # seconds for the phone to come back
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


def genre_counts(db_path: Path) -> dict[str, int]:
    """Songs per genre in the catalogue, most first (read-only). A "Pop, Rock"
    tag counts as Pop, its first genre, as the build and the playlists read it."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT genre FROM archive WHERE status = 'CATALOGUED'").fetchall()
    finally:
        conn.close()
    counts = Counter(primary_genre(g) for (g,) in rows if g and primary_genre(g))
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _picked(src: Path, genres: tuple[str, ...]) -> list[Path]:
    """The songs of *genres* in the edition, read from its genre playlists
    (written after every build), and those playlists themselves."""
    out: list[Path] = []
    for g in genres:
        pl = src / "Playlists" / f"{_safe_genre(g)}.m3u8"
        if not pl.is_file():
            continue
        out.append(pl)
        # shortcut: two genres whose names differ only by "/" vs "-" share one
        # playlist file; none do today (stages/playlist.py _safe_genre).
        for line in pl.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip() and not line.startswith("#"):
                f = Path(os.path.normpath(pl.parent / line.strip()))
                if f.is_relative_to(src) and f.is_file():
                    out.append(f)
    return sorted(set(out))


def _files(root: Path, genres: tuple[str, ...] = ()) -> list[Path]:
    if genres:
        return _picked(root, genres)
    return [p for p in root.rglob("*") if p.is_file() and not p.name.endswith(TMP_SUFFIX)]


def _size(root: Path, genres: tuple[str, ...] = ()) -> tuple[int, int]:
    files = _files(root, genres)
    return len(files), sum(p.stat().st_size for p in files)


def _wipe(root: Path) -> None:
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _missing_bytes(src: Path, dest: Path, genres: tuple[str, ...] = ()) -> int:
    """Bytes of *src* songs not on the phone yet (missing, or a different size)."""
    total = 0
    for f in _files(src, genres):
        size = f.stat().st_size
        there = dest / f.relative_to(src)
        if not (there.is_file() and there.stat().st_size == size):
            total += size
    return total


def _build(budget_gb: float, genres: tuple[str, ...] = ()) -> int:
    cmd = [
        sys.executable,
        "-m",
        "musaeus",
        "edition-build",
        "iphone",
        "--budget-gb",
        f"{budget_gb}",
    ]
    for g in genres:
        cmd += ["--genre", g]
    return subprocess.run(cmd).returncode


def _rsync(src: Path, mount: Path, genres: tuple[str, ...] = ()) -> int:
    # Not -a: its -p chmods each temp file, which ifuse does not do, so every
    # file failed with "Function not implemented (38)" (2026-11-09).
    cmd = ["rsync", "-rt", "--info=progress2", f"--exclude=*{TMP_SUFFIX}"]
    if not genres:
        return subprocess.run([*cmd, f"{src}/", f"{mount}/"]).returncode
    # "./": rsync drops a --files-from line starting with "#" or ";" as a comment.
    names = "\n".join(f"./{f.relative_to(src).as_posix()}" for f in _files(src, genres))
    return subprocess.run(
        [*cmd, "--files-from=-", f"{src}/", f"{mount}/"], input=names, text=True
    ).returncode


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"


def sync(
    src: Path, mount: Path, ask: Callable[[str], str] = input, genres: tuple[str, ...] = ()
) -> int:
    """Run the whole sync; 0 when the songs are on the phone.

    *genres*: only these go on the phone (Grey, 2026-11-10); with yes they
    replace what VLC holds. The edition keeps its copies of other genres.
    """
    print("iPhone sync\n" + "=" * 62)
    if genres:
        print("  Genres: " + ", ".join(genres))
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
        rc = _build(budget_gb, genres)
        if rc != 0:
            print("  The edition build failed; nothing on the phone was changed.")
            return rc

    if not src.is_dir():
        print(f"  The iPhone edition does not exist yet at {src}.")
        return 1
    for g in genres:  # said once, not skipped silently (Fable review of #315)
        if not (src / "Playlists" / f"{_safe_genre(g)}.m3u8").is_file():
            print(f"  No {g} song is in the iPhone edition.")
    if genres and not _files(src, genres):
        print("  The iPhone edition has no songs of those genres yet. Run again and type yes.")
        return 1
    # A build can end over its budget (CodeRabbit on #310): never clear VLC for
    # a copy that cannot fit.
    if wipe and _size(src, genres)[1] > free + held - HEADROOM:
        print(
            f"  The iPhone edition is {_gb(_size(src, genres)[1])}; after clearing VLC the phone "
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
            need = _missing_bytes(src, mount, genres)
            if need > free - HEADROOM:
                print(
                    f"  The songs not on the phone yet need {_gb(need)}; the phone has "
                    f"{_gb(free)} free and keeps {_gb(HEADROOM)} spare. Nothing was copied.\n"
                    "  Run again and type yes to clear VLC, or free space on the phone."
                )
                return 1
        print("  Copying ...")
        rc = _rsync(src, mount, genres)
        for attempt in range(1, RECONNECTS + 1):
            if rc not in LINK_LOST:
                break
            # rsync skips what already arrived (same size and time), so a new
            # try only copies the rest.
            print(f"  The phone dropped the link. Reconnecting ({attempt} of {RECONNECTS}) ...")
            _unmount(mount)
            time.sleep(RECONNECT_WAIT)
            if not (_attached() and _mount(mount)):
                print("  The phone is not back. Plug it in, unlock it, and leave it alone.")
                break
            rc = _rsync(src, mount, genres)
    finally:
        _unmount(mount)
    if rc == 0:
        print("  Done: the songs are in VLC on the phone.")
    else:
        print(f"  rsync stopped ({rc}). Run again and type no: songs already there are kept.")
    return rc
