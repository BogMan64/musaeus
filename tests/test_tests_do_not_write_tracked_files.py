"""A test run must leave the working tree as it found it.

The P0-19 gates used to write their evidence straight into
`docs/p0_evidence/P0-19/`, which is tracked. Every `pytest` therefore
modified 11 committed files. Two things went wrong with that, and only the
second one is obvious:

1. The working tree was dirty after every run. On 2026-09-23 that blocked a
   `git checkout main` outright -- git refused rather than discard changes
   nobody had asked for, in the middle of a merge.
2. Those files are a RECORD of the 2026-09-08 rehearsal. Regenerating them on
   every run silently overwrote the history they exist to preserve, so the
   prose in README.md described a run whose evidence had been replaced.

Writing is now opt-in through MUSAEUS_WRITE_EVIDENCE. Reading is not: G11
still needs the committed baseline, and that asymmetry is the whole point.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"

#: Tracked directories a test run must never write into as a side effect.
PROTECTED = ("docs/p0_evidence",)


def test_the_rehearsal_writes_outside_the_repo_by_default():
    assert "MUSAEUS_WRITE_EVIDENCE" not in os.environ, (
        "this test describes the DEFAULT; run it without MUSAEUS_WRITE_EVIDENCE set"
    )
    from tests.test_p0_19_release_rehearsal import EVIDENCE_DIR, EVIDENCE_OUT

    assert EVIDENCE_OUT != EVIDENCE_DIR, (
        "the gates are writing into the tracked evidence directory again; "
        "every pytest run will dirty the working tree"
    )
    assert REPO_ROOT not in EVIDENCE_OUT.parents, (
        f"evidence is being written inside the repo ({EVIDENCE_OUT}), which is "
        "what dirties the tree -- it belongs in a temp directory"
    )


def test_no_test_module_writes_into_a_protected_directory():
    """A literal path into a tracked docs directory, passed to a write call.

    String constants are checked, not prose: a docstring that merely mentions
    the path is an ast.Constant too, so only constants that reach a write are
    considered -- `write_text`, `open(..., "w")`, `mkdir` and friends.
    """
    writers = {"write_text", "write_bytes", "mkdir", "touch", "open", "makedirs"}
    offenders: list[str] = []

    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in writers:
                continue
            for const in [n for n in ast.walk(node) if isinstance(n, ast.Constant)]:
                if isinstance(const.value, str) and any(p in const.value for p in PROTECTED):
                    offenders.append(f"{path.name}:{node.lineno} writes to {const.value!r}")

    assert not offenders, (
        "a test writes directly into a tracked directory; route it through an "
        "opt-in output path instead:\n  " + "\n  ".join(offenders)
    )
