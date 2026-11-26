"""Is a finished file locked against writing?

The archive is protected with the immutable flag (`chattr +i`, `lsattr` shows
`i`). That is the right default: a tool cannot rename over, retag or delete a
master by accident, and the 2026-11-13 incident (1,197 masters rewritten by an
unreviewed script) is exactly what the flag stops.

It also means a tag writer cannot work. tagger, forge, bpm, identity_tags and
albumart all finish by renaming a written copy over the master
(`safe_save.change_beside`), and the kernel refuses that rename. Until
2026-11-25 nothing said so: `write_beside` swallowed the failure and returned
"not written", so the stage reported `errors: 7,975` with no reason, after
copying every master once for nothing. Masked only because the last tagger run
had 0 changes.

Measured on this machine, 2026-11-25:
    lsattr over Libraries/ALAC-Archival   7,975 of 7,975 files carry `i`
    opening one for writing               "Operation not permitted"

Reading the flag needs no root and no `lsattr` process: FS_IOC_GETFLAGS on an
open descriptor returns the inode flags. A filesystem that does not support
the ioctl (or a platform without it) answers "not locked" -- this module is a
warning system, never the thing that protects a file.
"""

from __future__ import annotations

import os
import shlex
import stat
import sys
from collections.abc import Iterable
from pathlib import Path

#: linux/fs.h -- FS_IOC_GETFLAGS and the immutable bit.
FS_IOC_GETFLAGS = 0x80086601
FS_IMMUTABLE_FL = 0x00000010

#: What counts as a music file for a scan. Artwork and playlists are not
#: written by the tag stages and would only slow the walk down.
AUDIO_SUFFIXES = frozenset({".m4a", ".mp4", ".alac", ".flac", ".mp3", ".wav", ".aiff"})

#: Windows has no O_NONBLOCK; 0 keeps the flag absent where it does not exist,
#: so a test that fakes linux on Windows still opens the file.
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def _get_flags(fd: int) -> int:
    """The inode flags for an open descriptor (Linux only)."""
    import fcntl
    import struct

    buf = struct.pack("i", 0)
    return int(struct.unpack("i", fcntl.ioctl(fd, FS_IOC_GETFLAGS, buf))[0])


def is_immutable(path: Path | str) -> bool:
    """True when the file carries the immutable flag.

    False on any doubt -- an unreadable file, a filesystem without the ioctl,
    a platform without the flag. A wrong "locked" would stop a run that could
    have worked; a wrong "not locked" only loses the early warning, and the
    write path still reports its own failure (safe_save.MastersLocked).
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        # O_NONBLOCK: a FIFO named like a song would otherwise block the scan
        # for ever, waiting for a writer that never comes. The regular-file
        # check below is what makes the ioctl meaningful anyway.
        fd = os.open(path, os.O_RDONLY | _NONBLOCK)
    except OSError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        return bool(_get_flags(fd) & FS_IMMUTABLE_FL)
    except OSError:
        return False
    finally:
        os.close(fd)


def unlock_hint(path: Path | str) -> str:
    """The command that unlocks a tree, named with the path it applies to.

    Quoted: an album folder named "Album Name" would otherwise print as two
    arguments and the copied command would fail (CodeRabbit, #331).
    """
    return f"sudo chattr -R -i {shlex.quote(str(path))}"


def lock_hint(path: Path | str) -> str:
    """The command that puts the flag back, quoted for the same reason."""
    return f"sudo chattr -R +i {shlex.quote(str(path))}"


def locked_in(
    root: Path | str, *, suffixes: Iterable[str] | None = AUDIO_SUFFIXES
) -> tuple[int, list[Path]]:
    """(count of locked files under *root*, up to three example paths)."""
    base = Path(root)
    if not base.is_dir():
        return 0, []
    wanted = {s.lower() for s in suffixes} if suffixes is not None else None
    n = 0
    examples: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in filenames:
            if wanted is not None and Path(name).suffix.lower() not in wanted:
                continue
            p = Path(dirpath) / name
            if is_immutable(p):
                n += 1
                if len(examples) < 3:
                    examples.append(p)
    return n, examples
