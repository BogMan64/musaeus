"""Change a finished file without ever saving the file itself.

mutagen rewrites the file it saves. A save stopped part way -- a kill, a
full disk, a power cut -- left a damaged file under its final name: a master
(review of #88, finding 3) or an edition copy that still read, marker and
all, and played broken (reviews of #127 and #129-#134). One way to do it,
shared: the change goes on a copy beside the file, the copy is checked and
flushed to disk, and only then takes the file's place, in one rename.

CLAUDE.md: existence is not completeness -- write aside, verify, then rename.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from collections.abc import Callable
from pathlib import Path


def change_beside(
    path: Path,
    change: Callable[[Path], None],
    *,
    suffix: str,
    check: Callable[[Path], None] | None = None,
) -> None:
    """Apply *change* to a copy of *path* named <name><suffix>, run *check* on
    it (raise to refuse), fsync it, rename it over *path*, fsync the folder.

    On any failure the copy is removed and *path* is as it was. A kill leaves
    at most the copy, which the next run replaces (or a sweep of *suffix*
    removes). The copy costs the file's size in writes: callers that change
    many files say so before they run.
    """
    tmp = path.with_name(path.name + suffix)
    shutil.copy2(path, tmp)  # replaces a copy a killed run left
    try:
        change(tmp)
        if check is not None:
            check(tmp)
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
