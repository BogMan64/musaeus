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
import errno
import os
import shlex
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from .immutability import is_immutable


class MastersLocked(RuntimeError):
    """A finished file refused the change because it is locked (+i).

    Raised BEFORE the copy is made, so a locked archive costs one failed open
    per file instead of one full copy per file. The stage runner shows this
    message as the stage's error, which is the whole point: before
    2026-11-25 the rename failed inside change_beside, write_beside returned
    "not written", and the stage printed `errors: 7,975` with no reason.
    """


def _locked_message(path: Path) -> str:
    folder = shlex.quote(str(path.parent))
    return (
        f"cannot change {path.name}: the file is immutable (chattr +i), so no "
        "rewrite can take its place. This is the archive's protection, not a "
        "fault. To change master tags: unlock its folder (or the whole archive) "
        f"with `sudo chattr -R -i {folder}`, run the stage, then lock it "
        f"again with `sudo chattr -R +i {folder}`."
    )


def _refused_message(path: Path, exc: OSError) -> str:
    folder = shlex.quote(str(path.parent))
    return (
        f"could not put the rewritten copy in place at {path.name}: {exc.strerror or exc}. "
        "The usual causes are the immutable flag (lsattr shows i) or a file owned "
        f"by another user; check with `lsattr {shlex.quote(str(path))}`. If it is locked, unlock "
        f"its folder (or the whole archive) with `sudo chattr -R -i {folder}`, "
        f"run the stage, then lock it again with `sudo chattr -R +i {folder}`."
    )


def fsync_file(path: Path | str) -> None:
    """Force a written, closed file's bytes to disk, by name.

    Windows flushes only a handle opened for writing: fsync on a read-only
    one raised "Bad file descriptor" there (Windows port, 2026-11-08). POSIX
    keeps read-only, which also works on a copy that inherited a read-only mode.
    """
    try:
        fd = os.open(path, os.O_RDWR if sys.platform == "win32" else os.O_RDONLY)
    except PermissionError as exc:
        # A file marked read-only (copy2 carries the flag) gives no writable
        # handle, and finalize deletes the source after this flush: clear the
        # flag around it (CodeRabbit, #295). Only that case: the flag is set and
        # the error is access denied (5) or carries no Windows code (os.open may
        # give errno only). A sharing violation (32, a scanner holding it) or a
        # file without the flag still stops the step.
        mode = os.stat(path).st_mode
        if not (
            sys.platform == "win32"
            and not mode & stat.S_IWRITE
            and getattr(exc, "winerror", None) in (None, 5)
        ):
            raise
        os.chmod(path, mode | stat.S_IWRITE)
        try:
            fd = os.open(path, os.O_RDWR)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            os.chmod(path, mode)
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(folder: Path | str) -> None:
    """Make a rename in *folder* durable: fsync the folder itself.

    Without it the rename can be lost even when the file's own data is safe.
    Windows cannot open a folder as a file, and NTFS journals the rename on
    its own, so there it does nothing (Windows port, 2026-11-08: four copies
    of this block raised after every successful save there).
    """
    if sys.platform == "win32":
        return
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def copy_to_new(src: Path | str, dst: Path | str) -> None:
    """Copy *src* (bytes, then mode and times) to a NEW file at *dst*.

    Whatever is at *dst* is not written through (Codex audit 2026-10-16, F1):
    shutil.copy2 followed a link planted there and overwrote the file it
    named. The name is cleared -- a link or hardlink loses its name, never its
    target's bytes -- and the file is created here with O_EXCL|O_NOFOLLOW: if
    anything claims the name again first, the open fails and nothing is
    written. The source is opened first, so a source that cannot be read
    leaves no handle and no file behind.
    """
    src, dst = Path(src), Path(dst)
    with open(src, "rb") as rf:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(dst)
        try:
            # Windows has no O_NOFOLLOW; there O_EXCL alone refuses a link at the name.
            no_follow = getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow, 0o600)
        except FileExistsError:
            raise OSError(f"{dst} was claimed by something else while it was being made") from None
        with os.fdopen(fd, "wb") as wf:
            shutil.copyfileobj(rf, wf)
    shutil.copystat(src, dst)


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
    # Before the copy, not after: a locked archive then costs one failed open
    # per file instead of a full copy per file thrown away (2026-11-25).
    if is_immutable(path):
        raise MastersLocked(_locked_message(path))
    try:
        # Inside the try: a copy cut short (disk full) is removed too
        # (review of #135-#139, finding 6). Never written through what was at
        # the name (copy_to_new; Codex audit 2026-10-16, F1).
        copy_to_new(path, tmp)
        change(tmp)
        if check is not None:
            check(tmp)
        fsync_file(tmp)
        try:
            os.replace(tmp, path)
        except OSError as exc:
            # The pre-check answers only where the ioctl works. An ACL, a
            # read-only mount or another owner arrives here instead, and the
            # bare "[Errno 1] Operation not permitted" never said which.
            if exc.errno in (errno.EPERM, errno.EACCES):
                raise MastersLocked(_refused_message(path, exc)) from exc
            raise
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    fsync_dir(path.parent)


def write_new(path: Path, data: bytes) -> None:
    """Write *data* as *path* without ever writing through what is there.

    A cover beside a song was written with write_bytes, which follows a link:
    a planted `<song>.musaeus_cover.jpg` or dangling `cover.jpg` link sent
    the image into a file elsewhere (Codex audit 2026-10-16, F1). The bytes
    go to a private, exclusively created file in the same folder, are flushed,
    and that file is renamed onto the name -- replacing a link, not following
    it.
    """
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(name, 0o644)
        os.replace(name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)
        raise


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
    except MastersLocked:
        # Not "the copy failed, file unchanged": the file IS unchanged because
        # the archive refused the rename, and the stage must say so (2026-11-25).
        raise
    except Exception:
        if raised:
            raise  # the writer's own error, as callers already handle it
        return failed  # the copy or its check failed; the file is as it was
    return result[-1]
