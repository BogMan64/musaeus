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
        fds, env = ml.handoff()
        handed = _child(runs, pass_fds=fds, env=env)
        assert handed.returncode == 0 and "HELD" in handed.stdout, handed.stderr
        stranger = _child(runs)
        assert stranger.returncode == ml.EXIT_BUSY, "the child's exit let go of the parent's hold"


def test_a_child_not_handed_the_hold_is_refused(tmp_path):
    runs = tmp_path / "RUNS"
    with ml.masters_lock(runs, exclusive=True, what="parent"):
        assert _child(runs).returncode == ml.EXIT_BUSY


def test_a_forged_number_is_not_the_lock(tmp_path):
    runs = tmp_path / "RUNS"
    other = open(tmp_path / "other.file", "a+")  # noqa: SIM115
    try:
        with ml.masters_lock(runs, exclusive=True, what="parent"):
            import os

            os.set_inheritable(other.fileno(), True)
            env = {**os.environ, ml._FD_ENV: str(other.fileno())}
            r = _child(runs, pass_fds=(other.fileno(),), env=env)
            assert r.returncode == ml.EXIT_BUSY
    finally:
        other.close()
