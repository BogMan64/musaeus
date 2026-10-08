"""Review of #87, findings 9 and 10 (2026-10-07): nothing stopped two jobs from
running at once. One lock on the masters now: exclusive for a job that changes
them, shared for a job that only reads them.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from musaeus.masters_lock import EXIT_BUSY, MastersBusy, holders, masters_lock

ROOT = Path(__file__).resolve().parent.parent


def test_a_job_that_changes_masters_excludes_every_other(tmp_path):
    with masters_lock(tmp_path, exclusive=True, what="musaeus pipeline (Act 2)"):
        with (
            pytest.raises(MastersBusy, match=r"musaeus pipeline \(Act 2\)"),
            masters_lock(tmp_path, exclusive=False, what="edition-build car"),
        ):
            pass
        with pytest.raises(MastersBusy), masters_lock(tmp_path, exclusive=True, what="bitrot"):
            pass


def test_readers_share(tmp_path):
    with masters_lock(tmp_path, exclusive=False, what="edition-build car"):
        with masters_lock(tmp_path, exclusive=False, what="edition-build iphone"):
            assert {h["what"] for h in holders(tmp_path)} == {
                "edition-build car",
                "edition-build iphone",
            }
        with (
            pytest.raises(MastersBusy),
            masters_lock(tmp_path, exclusive=True, what="musaeus pipeline"),
        ):
            pass
    assert holders(tmp_path) == []


def test_the_bitrot_check_waits_for_a_running_job(tmp_path):
    """Through the real command: the monthly wrapper retries on EXIT_BUSY."""
    env = {**os.environ, "MUSAEUS_VAULT_ROOT": str(tmp_path),
           "MUSAEUS_DB_PATH": str(tmp_path / "musaeus.db"),
           "MUSAEUS_NO_SLEEP_INHIBIT": "1", "MUSAEUS_NO_IDLE_THROTTLE": "1"}  # fmt: skip
    runs_root = tmp_path / "RUNS"
    with masters_lock(runs_root, exclusive=True, what="musaeus pipeline (a stand-in run)"):
        run = subprocess.run(
            [sys.executable, "-m", "musaeus.cli", "bitrot"], cwd=ROOT, env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120,
        )  # fmt: skip

    assert run.returncode == EXIT_BUSY, run.stdout[-500:] + run.stderr[-500:]
    assert "a stand-in run" in run.stderr
