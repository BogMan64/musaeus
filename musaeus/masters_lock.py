"""One lock on the masters, taken by every job that reads or changes them.

Review of #87, findings 9 and 10 (2026-10-07): nothing stopped two jobs from
running at once. The P0 scope lock (`musaeus/safety/lock.py`) only ever locks
test folders, so on the live vault preflight always found it free; and the
monthly wrappers guessed from the process list (`busy()`), which missed cases
`scripts/musaeus_running.sh` caught and the other way round.

A job that changes masters -- a pipeline run, the bit-rot repair, the delete,
swap and merge tools -- holds it exclusively. The swap tool hands its hold to
the delete tool it runs (handoff), so --retire is locked from choosing to
deleting. A job that only reads them -- an
edition build, the music backup -- holds it shared, so builds still run side by
side but nothing changes a master under them. `fcntl.flock`: the kernel frees it
when the holder dies, `kill -9` included. Who holds it is written beside it for
the message, never for the decision.

A job that cannot get it says who has it and exits with EXIT_BUSY (75, "try
again later"); the monthly wrappers retry on that code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    import _winapi
    import msvcrt

    from .file_lock import try_lock_handoff, unlock_handoff

from .file_lock import process_alive, try_lock, unlock

EXIT_BUSY = 75
LOCK_NAME = "masters.lock"
_HOLDERS = "masters.holders"
_held: list = []  # hold_for_process keeps its file open here until exit
#: (open lock file, exclusive) for every hold this process has now.
_open: list = []
#: The fd number (a handle on Windows) of a parent's held lock file, passed
#: down by handoff().
_FD_ENV = "MUSAEUS_MASTERS_LOCK_FD"
#: Windows: an inherited handle on the parent process holding the lock it
#: handed down. A handle, not a pid: Windows never reuses a pid while a handle
#: on that process is open (CodeRabbit on #309).
_PARENT_ENV = "MUSAEUS_MASTERS_LOCK_PARENT"
_SYNCHRONIZE = 0x00100000  # enough to wait on it, nothing more


def handoff() -> dict[str, Any]:
    """subprocess.run() arguments for a child that must run under THIS
    process's exclusive hold, taking no lock of its own (Codex audit
    2026-10-16, F9).

    The swap tool chooses what to retire under the lock and the delete tool
    deletes it; let go in between, another job could change the choice. The
    child is handed the very open file the lock is on: flock on the same
    open file description is the same lock, so the child's exclusive request
    succeeds while the parent -- waiting on it -- still holds it."""
    for fh, exclusive in _open:
        if exclusive:
            return _hand_down(fh)
    return {"env": dict(os.environ)}


def _hand_down(fh) -> dict[str, Any]:
    if sys.platform == "win32":
        # Windows has no pass_fds: the handle itself is inherited, and only it.
        h = msvcrt.get_osfhandle(fh.fileno())
        os.set_handle_inheritable(h, True)
        me = _winapi.GetCurrentProcess()
        # shortcut: one small handle per handoff stays open; fine for one child per run.
        parent = _winapi.DuplicateHandle(me, me, me, _SYNCHRONIZE, True, 0)
        return {
            "close_fds": True,
            "startupinfo": subprocess.STARTUPINFO(lpAttributeList={"handle_list": [h, parent]}),
            "env": {**os.environ, _FD_ENV: str(h), _PARENT_ENV: str(parent)},
        }
    fd = fh.fileno()
    os.set_inheritable(fd, True)
    return {"pass_fds": (fd,), "env": {**os.environ, _FD_ENV: str(fd)}}


def _own_copy(raw: int) -> int:
    """A new fd on the handed-down file. Closing it (a nested hold ending)
    never closes the parent's (re-review of #202)."""
    if sys.platform == "win32":
        me = _winapi.GetCurrentProcess()
        h = _winapi.DuplicateHandle(me, raw, me, 0, False, _winapi.DUPLICATE_SAME_ACCESS)
        return msvcrt.open_osfhandle(h, 0)
    return os.dup(raw)


def _inherited(lock_file: Path):
    """The parent's lock file, if one was handed down AND really is this lock
    file -- a stray or forged number opens nothing."""
    raw = os.environ.get(_FD_ENV, "")
    if not raw.isdigit():
        return None
    try:
        fd = _own_copy(int(raw))
    except (OSError, OverflowError):
        return None
    try:
        same = os.path.samestat(os.fstat(fd), os.stat(lock_file))
    except OSError:
        same = False
    if not same:
        os.close(fd)
        return None
    return os.fdopen(fd, "a+")


def _parent_alive() -> bool:
    """Windows: the parent that handed down the lock still runs."""
    raw = os.environ.get(_PARENT_ENV, "")
    if sys.platform == "win32" and raw.isdigit():
        try:
            return _winapi.WaitForSingleObject(int(raw), 0) == _winapi.WAIT_TIMEOUT
        except (OSError, OverflowError):
            return False
    return False


def _take(fh, *, exclusive: bool, handed_down: bool) -> None:
    """Lock, or raise BlockingIOError. flock: a handed-down file shares the
    parent's lock, and keeps it if the parent dies. Windows refuses a second
    lock even on the parent's own file, and frees the parent's lock when the
    parent dies (CodeRabbit on #309). So there a handed-down child holds the
    handoff byte, and every other holder checks that byte is free."""
    if sys.platform == "win32":
        if handed_down:
            try_lock_handoff(fh, exclusive=True)
            # The parent may have died before this child took the byte; another
            # job may hold the lock now (CodeRabbit on #309). Alive, the parent
            # still holds it, so nobody else can.
            if not _parent_alive():
                unlock_handoff(fh)
                raise BlockingIOError("the parent that handed down the lock has ended")
            return
        try_lock(fh, exclusive=exclusive)
        try:
            # No race: a child takes the byte only while its parent holds the lock.
            try_lock_handoff(fh, exclusive=False)
        except BlockingIOError:
            unlock(fh)
            raise
        unlock_handoff(fh)
    else:
        try_lock(fh, exclusive=exclusive)


class MastersBusy(RuntimeError):
    """Another job holds the masters lock in a mode this one cannot share."""


def _lock_dir(runs_root: Path | str) -> Path:
    d = Path(runs_root) / "locks"
    os.makedirs(d, exist_ok=True)
    return d


def holders(runs_root: Path | str) -> list[dict]:
    """Who holds the lock now, as far as their records say (dead ones dropped)."""
    out = []
    for f in sorted((_lock_dir(runs_root) / _HOLDERS).glob("*.json")):
        try:
            h = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if process_alive(int(h.get("pid", 0))):
            out.append(h)
    return out


def _describe(hs: list[dict]) -> str:
    if not hs:
        return "another MUSAEUS job"
    return ", ".join(f"{h.get('what', '?')} (pid {h.get('pid')}, {h.get('mode')})" for h in hs)


@contextmanager
def masters_lock(runs_root: Path | str, *, exclusive: bool, what: str) -> Iterator[None]:
    """Hold the masters lock for the block, or raise MastersBusy at once."""
    lock_dir = _lock_dir(runs_root)
    # Exclusive only: a shared request on the parent's open file would turn
    # the parent's exclusive hold into a shared one.
    inherited = _inherited(lock_dir / LOCK_NAME) if exclusive else None
    fh = inherited or open(lock_dir / LOCK_NAME, "a+", encoding="utf-8")  # noqa: SIM115 -- held for the block
    try:
        _take(fh, exclusive=exclusive, handed_down=inherited is not None)
    except BlockingIOError:
        fh.close()
        raise MastersBusy(
            f"the masters are in use by {_describe(holders(runs_root))}; try again when it "
            f"has finished"
        ) from None
    record = lock_dir / _HOLDERS / f"{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
    record.parent.mkdir(exist_ok=True)
    record.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "what": what,
                "mode": "exclusive" if exclusive else "shared",
                "since": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        ),  # fmt: skip
        encoding="utf-8",
    )
    _open.append((fh, exclusive))
    try:
        yield
    finally:
        _open.remove((fh, exclusive))
        record.unlink(missing_ok=True)
        if inherited is None:  # never unlock the parent's hold
            unlock(fh)
        elif sys.platform == "win32":
            unlock_handoff(fh)
        fh.close()


def hold_for_process(*, exclusive: bool, what: str, runs_root: Path | str | None = None) -> None:
    """For a script: hold the lock until the process ends, or exit EXIT_BUSY."""
    if runs_root is None:
        from .config import get_config

        runs_root = get_config().runs_root
    cm = masters_lock(runs_root, exclusive=exclusive, what=what)
    try:
        cm.__enter__()
    except MastersBusy as exc:
        print(f"NOT RUN: {exc}", file=sys.stderr)
        sys.exit(EXIT_BUSY)
    _held.append(cm)  # released by the kernel when the process exits
