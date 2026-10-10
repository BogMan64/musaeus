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
from typing import TypeVar


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
    try:
        # Inside the try: a copy cut short (disk full) is removed too
        # (review of #135-#139, finding 6). Replaces a copy a killed run left.
        shutil.copy2(path, tmp)
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


T = TypeVar("T")


class _WriterRefused(Exception):
    pass


def write_beside(
    path: Path,
    write: Callable[[Path], T],
    *,
    failed: T,
    ok: Callable[[T], bool] = bool,
    suffix: str = ".tagging",
) -> T:
    """Run a tag writer on a copy beside *path* instead of on *path* itself.

    *write* gets the copy's path and returns its usual result. The copy takes
    the place of *path* only if ok(result) and the copy still reads, with the
    same length; then the writer's result is returned. Otherwise *path* is as
    it was, and the writer's result (or *failed*, when something else went
    wrong) is returned. For the stage writers that saved masters in place --
    Forge, Tagger, BPM, IdentityTag (review of slice A, findings 4-6): a kill,
    power cut or full disk mid-save left a damaged master under its name.
    """
    import mutagen

    result: list[T] = []
    raised: list[Exception] = []

    def change(tmp: Path) -> None:
        try:
            result.append(write(tmp))
        except Exception as exc:
            raised.append(exc)
            raise
        if not ok(result[-1]):
            raise _WriterRefused

    def same_length(tmp: Path) -> None:
        before, after = mutagen.File(path), mutagen.File(tmp)
        if after is None or (
            before is not None and abs(after.info.length - before.info.length) > 0.01
        ):
            raise ValueError(f"the tagged copy of {path.name} does not read as the same audio")

    try:
        change_beside(path, change, suffix=suffix, check=same_length)
    except _WriterRefused:
        return result[-1]
    except Exception:
        if raised:
            raise  # the writer's own error, as callers already handle it
        return failed  # the copy or its check failed; the file is as it was
    return result[-1]
