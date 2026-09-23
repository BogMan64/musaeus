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


_WRITERS = {"write_text", "write_bytes", "mkdir", "touch", "makedirs", "open"}


def _path_text(node: ast.AST) -> str | None:
    """A path expression as text, so a protected path is visible however built.

    "docs/p0_evidence" stays itself; `REPO_ROOT / "docs" / "p0_evidence"`
    becomes "*/docs/p0_evidence". The first version of this guard matched only
    single string constants, and the bug it was written for built its path
    the second way -- so when the old behaviour was reinstated on purpose,
    this check PASSED. The 2026-09-23 review found it; the test run had shown
    it ("1 failed, 1 passed") and nobody read the second number.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return f"{_path_text(node.left) or '*'}/{_path_text(node.right) or '*'}"
    return None


def _is_write(call: ast.Call) -> bool:
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name not in _WRITERS:
        return False
    if name != "open":
        return True
    # open() reads by default; only a write mode counts.
    mode = call.args[1] if len(call.args) > 1 else None
    for kw in call.keywords:
        if kw.arg == "mode":
            mode = kw.value
    return (
        isinstance(mode, ast.Constant)
        and isinstance(mode.value, str)
        and any(c in mode.value for c in "wax+")
    )


def _offenders_in(source: str, filename: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Call) or not _is_write(node):
            continue
        for sub in ast.walk(node):
            text = _path_text(sub)
            if text and any(p in text for p in PROTECTED):
                found.append(f"{filename}:{node.lineno} writes to {text!r}")
                break
    return found


def test_no_test_module_writes_into_a_protected_directory():
    """Every .py under tests/ -- conftest.py and helpers included.

    The first version scanned only tests/test_*.py, non-recursively, which
    skipped conftest.py, disposable_vault.py and transport_denial.py: all of
    them run in every session.

    What this CANNOT see is a path stored in a variable and written later
    (`EVIDENCE_DIR.mkdir()`). That needs data flow, not a syntax walk. The CI
    step "Tests left the working tree as they found it" covers it by
    measuring the tree itself, which no amount of source-reading can match.
    """
    offenders: list[str] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if "__pycache__" in path.parts or path.resolve() == Path(__file__).resolve():
            continue
        offenders += _offenders_in(path.read_text(encoding="utf-8"), path.name)

    assert not offenders, (
        "a test writes directly into a tracked directory; route it through an "
        "opt-in output path instead:\n  " + "\n  ".join(offenders)
    )


def test_the_scanner_sees_a_path_built_with_slashes():
    # The exact idiom that fooled the first version of this guard.
    src = (
        "from pathlib import Path\n"
        'ROOT = Path("r")\n'
        '(ROOT / "docs" / "p0_evidence" / "P0-19" / "G1.txt").write_text("x")\n'
    )
    assert _offenders_in(src, "synthetic.py")


def test_the_scanner_ignores_reads_and_prose():
    # A docstring is an ast.Constant too; prose about the path must not trip it,
    # and neither may reading the committed record, which G11 has to do.
    src = (
        '"""Writes to docs/p0_evidence are forbidden."""\n'
        "from pathlib import Path\n"
        'open("docs/p0_evidence/P0-19/G11_baseline_start.txt").read()\n'
        '(Path("r") / "docs" / "p0_evidence" / "G11.txt").read_text()\n'
    )
    assert not _offenders_in(src, "synthetic.py")
