"""One home for the `... and N more` wording.

head_with_remainder() centralised the ARITHMETIC on 2026-09-04 and the
rendering stayed copied. The review that caught this (P3-1) counted seven
copies; by the time it was acted on, on 2026-09-05, there were SEVENTEEN --
across cli, console, handoff and eleven stages.

That growth between finding and fixing is the argument for this file.
CLAUDE.md, under "Why the linter will not save you", is explicit: write an
AST guard whenever you consolidate a concept, because it is the only thing
that catches the next copy. Modelled on test_only_db_may_alter_a_table.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
#: The one legitimate home.
_OWNER = "context.py"


def _string_literals(tree: ast.AST):
    """Every string in an EXECUTABLE position, with its line.

    Docstrings and other bare string statements are prose -- this module's
    own docstring quotes the idiom, and head_with_remainder's docstring
    discusses it at length. A naive walk flags both.
    """
    prose = {
        id(n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
    }
    for node in ast.walk(tree):
        if id(node) in prose:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value, node.lineno
        elif isinstance(node, ast.JoinedStr):
            joined = "".join(
                v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            )
            yield joined, node.lineno


def test_the_elision_wording_lives_in_exactly_one_place() -> None:
    """Fail the eighteenth copy at CI instead of finding it in a review."""
    offenders: list[str] = []
    for path in sorted((_ROOT / "musaeus").rglob("*.py")):
        if path.name == _OWNER:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover
            continue
        for text, lineno in _string_literals(tree):
            low = text.lower()
            # An f-string's literal parts around `{n}` leave "... and " and
            # " more" adjacent in the joined text.
            if "... and " in low and "more" in low:
                offenders.append(f"{path.relative_to(_ROOT)}:{lineno}")
    assert not offenders, (
        "`... and N more` rendered outside musaeus/context.py — call "
        f"context.elision() instead: {offenders}"
    )


def test_the_guard_can_actually_see_a_violation() -> None:
    """A guard nobody has watched fail is a guard nobody should trust.

    The first draft of the ALTER TABLE guard this is modelled on passed
    because it was looking in the wrong place; this asserts the detector
    fires on a synthetic copy rather than assuming it would.
    """
    tree = ast.parse('x = f"  ... and {n} more"\n')
    hits = [t for t, _ in _string_literals(tree)
            if "... and " in t.lower() and "more" in t.lower()]
    assert hits, "the detector does not recognise the very pattern it guards"


def test_prose_about_the_idiom_is_not_flagged() -> None:
    """Docstrings discuss this wording on purpose and must stay legal."""
    tree = ast.parse('def f():\n    """prints ... and N more when truncated"""\n    return 1\n')
    hits = [t for t, _ in _string_literals(tree)
            if "... and " in t.lower() and "more" in t.lower()]
    assert not hits, "a docstring mentioning the idiom must not fail the build"
