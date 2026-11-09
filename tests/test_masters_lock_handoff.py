"""A parent's exclusive masters hold, handed to the child it runs (review of #202).

The swap tool chooses what to retire and the delete tool deletes it. The child
asking for the lock afresh was refused by its own parent (Codex audit F9); the
parent letting go first opened a window in which another job could change the
choice. The child is handed the parent's open lock file instead.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from musaeus import masters_lock as ml

CHILD = (
    "import sys; from musaeus.masters_lock import hold_for_process; "
    "hold_for_process(exclusive=True, what='child', runs_root=sys.argv[1]); print('HELD')"
)


def _child(runs: Path, **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", CHILD, str(runs)], capture_output=True, text=True,
        cwd=Path(__file__).resolve().parents[1], timeout=60, **kw,
    )  # fmt: skip


def test_a_child_handed_the_hold_runs_and_the_parent_still_holds_after(tmp_path):
    runs = tmp_path / "RUNS"
    with ml.masters_lock(runs, exclusive=True, what="parent"):
        handed = _child(runs, **ml.handoff())
        assert handed.returncode == 0 and "HELD" in handed.stdout, handed.stderr
        stranger = _child(runs)
        assert stranger.returncode == ml.EXIT_BUSY, "the child's exit let go of the parent's hold"


def test_a_child_not_handed_the_hold_is_refused(tmp_path):
    runs = tmp_path / "RUNS"
    with ml.masters_lock(runs, exclusive=True, what="parent"):
        assert _child(runs).returncode == ml.EXIT_BUSY


def test_a_forged_number_is_not_the_lock(tmp_path):
    runs = tmp_path / "RUNS"
    other = open(tmp_path / "other.file", "a+", encoding="utf-8")  # noqa: SIM115
    try:
        with ml.masters_lock(runs, exclusive=True, what="parent"):
            r = _child(runs, **ml._hand_down(other))
            assert r.returncode == ml.EXIT_BUSY
    finally:
        other.close()


CHILD_STAYS = (
    "import pathlib, sys, time; from musaeus.masters_lock import hold_for_process; "
    "hold_for_process(exclusive=True, what='child', runs_root=sys.argv[1]); "
    "pathlib.Path(sys.argv[2]).write_text('HELD'); time.sleep(60)"
)
PARENT_DIES = (
    "import os, pathlib, subprocess, sys, time; from musaeus import masters_lock as ml\n"
    "runs, mark = sys.argv[1], pathlib.Path(sys.argv[2])\n"
    "with ml.masters_lock(runs, exclusive=True, what='parent'):\n"
    "    child = subprocess.Popen([sys.executable, '-c', sys.argv[3], runs, str(mark)],\n"
    "        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
    "        **ml.handoff())\n"
    "    for _ in range(600):\n"
    "        if mark.exists(): break\n"
    "        time.sleep(0.1)\n"
    "    print(child.pid, flush=True)\n"
    "    os._exit(0)  # dies holding the lock; the child runs on\n"
)


def test_a_dead_parent_does_not_free_the_masters_while_its_child_runs(tmp_path):
    """CodeRabbit on #309: Windows frees a lock when the process that took it
    dies, even while a child still has the same file open. Another job must
    still be refused until the handed-down child has finished too."""
    import os
    import signal
    import time

    runs, mark = tmp_path / "RUNS", tmp_path / "child_holds"
    parent = subprocess.run(
        [sys.executable, "-c", PARENT_DIES, str(runs), str(mark), CHILD_STAYS],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1], timeout=90,
    )  # fmt: skip
    assert mark.exists(), parent.stderr
    pid = int(parent.stdout.split()[0])
    try:
        assert _child(runs).returncode == ml.EXIT_BUSY, "another job ran beside the child"
    finally:
        os.kill(pid, signal.SIGTERM)
    for _ in range(50):  # the child is gone: the masters are free again
        if _child(runs).returncode == 0:
            break
        time.sleep(0.2)
    else:
        raise AssertionError("the masters stayed locked after the child ended")
