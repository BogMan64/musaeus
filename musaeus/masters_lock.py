"""One lock on the masters, taken by every job that reads or changes them.

Review of #87, findings 9 and 10 (2026-10-07): nothing stopped two jobs from
running at once. The P0 scope lock (`musaeus/safety/lock.py`) only ever locks
test folders, so on the live vault preflight always found it free; and the
monthly wrappers guessed from the process list (`busy()`), which missed cases
`scripts/musaeus_running.sh` caught and the other way round.

A job that changes masters -- a pipeline run, the bit-rot repair, the delete,
swap and merge tools -- holds it exclusively. A job that only reads them -- an
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
    fh = open(lock_dir / LOCK_NAME, "a+")  # noqa: SIM115 -- held for the block
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
    try:
        yield
    finally:
        record.unlink(missing_ok=True)
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
