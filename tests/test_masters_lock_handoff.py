"""A parent's exclusive masters hold, handed to the child it runs (review of #202).

The swap tool chooses what to retire and the delete tool deletes it. The child
asking for the lock afresh was refused by its own parent (Codex audit F9); the
parent letting go first opened a window in which another job could change the
choice. The child is handed the parent's open lock file instead.
"""

from __future__ import annotations

import contextlib
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


LATE_CHILD = (
    "import pathlib, sys, time; from musaeus.masters_lock import hold_for_process\n"
    "go, out = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])\n"
    "for _ in range(600):\n"
    "    if go.exists(): break\n"
    "    time.sleep(0.1)\n"
    "try:\n"
    "    hold_for_process(exclusive=True, what='late child', runs_root=sys.argv[1])\n"
    "    out.write_text('HELD')\n"
    "except SystemExit as e:\n"
    "    out.write_text(str(e.code))\n"
)
PARENT_DIES_AT_ONCE = (
    "import os, subprocess, sys; from musaeus import masters_lock as ml\n"
    "with ml.masters_lock(sys.argv[1], exclusive=True, what='parent'):\n"
    "    child = subprocess.Popen([sys.executable, '-c', sys.argv[2], *sys.argv[1:2], *sys.argv[3:]],\n"
    "        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
    "        **ml.handoff())\n"
    "    print(child.pid, flush=True)\n"
    "    os._exit(0)  # dies before the child has even asked for the lock\n"
)


def test_a_child_whose_parent_died_before_it_started_does_not_run_beside_another_job(tmp_path):
    """CodeRabbit on #309, round 2: the parent dies, another job takes the
    lock, and only then does the handed-down child ask. Either the masters
    stayed locked (flock: the child's inherited file keeps the lock), or the
    child must be refused."""
    import os
    import signal
    import time

    runs, go, out = tmp_path / "RUNS", tmp_path / "go", tmp_path / "child_said"
    parent = subprocess.run(
        [sys.executable, "-c", PARENT_DIES_AT_ONCE, str(runs), LATE_CHILD, str(go), str(out)],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1], timeout=90,
    )  # fmt: skip
    pid = int(parent.stdout.split()[0])
    try:
        with ml.masters_lock(runs, exclusive=True, what="another job"):
            go.touch()
            for _ in range(300):
                if out.exists() and out.read_text():
                    break
                time.sleep(0.1)
            assert out.read_text() == str(ml.EXIT_BUSY), "the child ran beside another job"
    except ml.MastersBusy:
        pass  # the masters stayed locked for the child: no race to lose
    finally:
        go.touch()
        with contextlib.suppress(OSError):  # already ended
            os.kill(pid, signal.SIGTERM)
