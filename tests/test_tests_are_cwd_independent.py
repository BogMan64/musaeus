"""No test may read a source file through a cwd-relative path.

P3-2, 2026-09-05. `Path("musaeus/cli.py").read_text()` raises
FileNotFoundError whenever pytest is invoked from anywhere but the repo
root -- a subdirectory, an IDE runner, a CI step with a different cwd.
pyproject sets `testpaths` but never pins cwd, so nothing guaranteed it.

The review flagged one instance. There were ELEVEN, across six files, all
silently dependent on being launched from the right directory. They now use
inspect.getsource(), which is cwd-independent and additionally scopes the
read to the module actually under test.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TESTS = _ROOT / "tests"


def _relative_source_reads(tree: ast.AST) -> list[int]:
    """Path("<relative>/...py") -- a literal path with no anchor.

    An absolute path, or one built from Path(__file__), is fine: those do
    not care where pytest was started. Only a bare relative literal does.
    """
    bad: list[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "Path" and node.args):
            continue
        arg = node.args[0]
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            continue
        val = arg.value
        if val.startswith("/") or val.startswith("~"):
            continue
        if not val.endswith(".py"):
            continue
        # Path("x.py") on its own is data; the hazard is reading through it.
        parent = None
        for outer in ast.walk(tree):
            for child in ast.iter_child_nodes(outer):
                if child is node:
                    parent = outer
        if isinstance(parent, ast.Attribute) and parent.attr.startswith("read_"):
            bad.append(node.lineno)
    return bad


def test_no_test_reads_source_through_a_relative_path() -> None:
    offenders: list[str] = []
    for path in sorted(_TESTS.glob("*.py")):
        if path.name == Path(__file__).name:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover
            continue
        for lineno in _relative_source_reads(tree):
            offenders.append(f"{path.relative_to(_ROOT)}:{lineno}")
    assert not offenders, (
        "test reads a source file through a cwd-relative path and will break "
        "when pytest is run from elsewhere — use inspect.getsource(module): "
        f"{offenders}"
    )


def test_the_guard_can_actually_see_a_violation() -> None:
    """A guard nobody has watched fail is a guard nobody should trust."""
    tree = ast.parse('src = Path("musaeus/cli.py").read_text()\n')
    assert _relative_source_reads(tree), (
        "the detector does not recognise the very pattern it guards"
    )


def test_an_anchored_path_is_allowed() -> None:
    """Path(__file__)-based reads are cwd-independent and must stay legal."""
    tree = ast.parse(
        'from pathlib import Path\n'
        'ROOT = Path(__file__).resolve().parent.parent\n'
        'src = (ROOT / "musaeus" / "cli.py").read_text()\n'
    )
    assert not _relative_source_reads(tree)
