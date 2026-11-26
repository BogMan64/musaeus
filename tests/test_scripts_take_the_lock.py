"""Every script that writes to the masters takes the masters lock.

Review of #87 (2026-10-07) made the lock, but only for the pipeline and the
tools that already had it. Measured 2026-11-25: 16 of 55 scripts wrote audio
or moved files with no lock at all, so a second job could run beside them --
exactly what the lock exists to stop.

Seven of them are fixed here. The eighth, merge_case_duplicate_albums, is
retired: it exits 2 before it can touch anything. The test runs each one for real, with the lock
already held by another process, and expects the refusal: exit 75 and the
one-line NOT RUN message. It is the wiring that matters, not the message.

A dry run stays lock-free on purpose. Asking "what would you do?" must
always be possible, even while a real job is running -- so the same test
without the write flag asserts the script got past the lock.

Fails on 6cad046 for the right reason: the script never looks at the lock,
so it does its work (or dies on the empty vault) instead of refusing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from musaeus.masters_lock import EXIT_BUSY, masters_lock

REPO = Path(__file__).resolve().parents[1]

#: script name -> the flag that turns it from a dry run into a real write.
WRITE_FLAGS = {
    "backfill_identity_tags": "--apply",
    "musaeus_migrate_to_archive": "--execute",
    "musaeus_relink_recovered_dupes": "--execute",
    "prune_recovery": "--apply",
    "repair_album_artist": "--apply",
    "repair_artist_casing": "--apply",
    "split_artist_sort_form": "--apply",
}


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "MUSAEUS_VAULT_ROOT": str(tmp_path / "vault"),
        "MUSAEUS_RUNS_ROOT": str(tmp_path / "RUNS"),
        "PYTHONPATH": str(REPO),
    }


def _run(script: str, *flags: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / f"{script}.py"), *flags],
        capture_output=True,
        text=True,
        env=_env(tmp_path),
        cwd=str(tmp_path),
        timeout=180,
        check=False,
    )


@pytest.mark.parametrize("script", sorted(WRITE_FLAGS))
def test_a_write_run_refuses_while_another_job_holds_the_lock(script, tmp_path):
    runs = tmp_path / "RUNS"
    runs.mkdir(parents=True)
    with masters_lock(runs, exclusive=True, what="test holder"):
        r = _run(script, WRITE_FLAGS[script], tmp_path=tmp_path)

    assert r.returncode == EXIT_BUSY, (
        f"{script} {WRITE_FLAGS[script]} ran while the masters were locked "
        f"(exit {r.returncode})\nstdout: {r.stdout[-400:]}\nstderr: {r.stderr[-400:]}"
    )
    assert "NOT RUN" in r.stderr


@pytest.mark.parametrize("script", sorted(WRITE_FLAGS))
def test_a_dry_run_is_not_blocked_by_the_lock(script, tmp_path):
    runs = tmp_path / "RUNS"
    runs.mkdir(parents=True)
    with masters_lock(runs, exclusive=True, what="test holder"):
        r = _run(script, tmp_path=tmp_path)

    assert r.returncode != EXIT_BUSY, f"{script} took the lock on a dry run"
    assert "NOT RUN" not in r.stderr
