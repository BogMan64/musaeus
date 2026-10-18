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

import fcntl
import json
import os
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

EXIT_BUSY = 75
LOCK_NAME = "masters.lock"
_HOLDERS = "masters.holders"
_held: list = []  # hold_for_process keeps its file open here until exit
#: (open lock file, exclusive) for every hold this process has now.
_open: list = []
#: The fd number of a parent's held lock file, passed down by handoff().
_FD_ENV = "MUSAEUS_MASTERS_LOCK_FD"


def handoff() -> tuple[tuple[int, ...], dict[str, str]]:
    """(pass_fds, env) for a child that must run under THIS process's
    exclusive hold, taking no lock of its own (Codex audit 2026-10-16, F9).

    The swap tool chooses what to retire under the lock and the delete tool
    deletes it; let go in between, another job could change the choice. The
    child is handed the very open file the lock is on: flock on the same
    open file description is the same lock, so the child's exclusive request
    succeeds while the parent -- waiting on it -- still holds it."""
    for fh, exclusive in _open:
        if exclusive:
            fd = fh.fileno()
            os.set_inheritable(fd, True)
            return (fd,), {**os.environ, _FD_ENV: str(fd)}
    return (), dict(os.environ)


def _inherited(lock_file: Path):
    """The parent's lock file, if one was handed down AND really is this lock
    file -- a stray or forged number opens nothing."""
    raw = os.environ.get(_FD_ENV, "")
    if not raw.isdigit():
        return None
    try:
        if not os.path.samestat(os.fstat(int(raw)), os.stat(lock_file)):
            return None
        return os.fdopen(int(raw), "a+")
    except OSError:
        return None


class MastersBusy(RuntimeError):
    """Another job holds the masters lock in a mode this one cannot share."""


def _lock_dir(runs_root: Path | str) -> Path:
    d = Path(runs_root) / "locks"
    os.makedirs(d, exist_ok=True)
    return d


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def holders(runs_root: Path | str) -> list[dict]:
    """Who holds the lock now, as far as their records say (dead ones dropped)."""
    out = []
    for f in sorted((_lock_dir(runs_root) / _HOLDERS).glob("*.json")):
        try:
            h = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if _alive(int(h.get("pid", 0))):
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
    fh = inherited or open(lock_dir / LOCK_NAME, "a+")  # noqa: SIM115 -- held for the block
    try:
        fcntl.flock(fh, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
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
            fcntl.flock(fh, fcntl.LOCK_UN)
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
