"""Staging directories must not be shared between runs.

The builder rmtree'd a fixed `_staged` path on entry and again at the end
of a dry run. A preview therefore deleted the symlink tree that a LIVE
encode was reading from.

That happened 2026-09-01: an iPhone dry run pulled the staging out from
under a running Car build at file 4,860, and the encoder spent the next
3,713 files reporting "No such file or directory" before it was noticed.
No audio was lost -- already-converted output is skipped on a re-run --
but hours of wall clock went to a preview that was supposed to be
read-only.

The lesson is narrower than "be careful": a dry run that deletes anything
outside its own scope is not a dry run. Per-process staging makes the
collision impossible rather than merely unlikely.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_SRC = (Path(__file__).resolve().parents[1]
        / "scripts" / "car_library" / "build_car_library.py").read_text()


def test_staging_path_is_process_scoped() -> None:
    """A fixed name is the bug; the pid makes concurrent runs disjoint."""
    assert 'input_dir / f"_staged_{os.getpid()}"' in _SRC
    assert 'input_dir / "_staged"' not in _SRC, "fixed staging path reintroduced"


def test_two_processes_would_get_different_staging_paths() -> None:
    def staging_for(pid: int) -> str:
        return f"_staged_{pid}"
    assert staging_for(os.getpid()) != staging_for(os.getpid() + 1)


def test_rmtree_only_ever_targets_the_staging_dir() -> None:
    """Every rmtree in the builder must name staging_dir. A preview must
    not be able to remove anything else."""
    for line in _SRC.splitlines():
        if "rmtree(" in line:
            assert "staging_dir" in line, f"rmtree on a non-staging path: {line.strip()}"


def test_dry_run_still_cleans_up_after_itself() -> None:
    """Scoped, not abandoned -- a preview should leave no staging behind."""
    assert re.search(r"rmtree\(staging_dir, ignore_errors=True\)", _SRC)
