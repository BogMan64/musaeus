"""Every `from scripts.X import ...` inside the package must resolve.

Commit bf948fc (2026-08-08) is titled "feat: Add idle-aware LUFS forge
utility script" and adds two files. It also DELETES six:

    scripts/musaeus_canon_review.py    299 lines
    scripts/musaeus_fix_mislabeled.py  247
    scripts/musaeus_report.py          359
    scripts/musaeus_spec_scout.py      295
    scripts/musaeus_upgrade_check.py   242
    scripts/resolve_near_dupes.py      378

Four of those were imported by cli.py. The imports are inside command
functions, so nothing failed at startup and no test touched them -- and
`musaeus report`, `musaeus spec-scout`, `musaeus upgrade-check` and
`musaeus canon-review` raised ModuleNotFoundError for a MONTH, while
`--help` kept listing them as available. They were found on 2026-09-07 by a
second Claude reading the tree, not by anything in this repo.

A deferred import is invisible to the import graph, so this test walks the
AST instead. It does not import or execute anything.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE = _ROOT / "musaeus"


def _script_imports() -> list[tuple[Path, int, str]]:
    """Every `from scripts.<mod> import ...` in the package, with location."""
    found: list[tuple[Path, int, str]] = []
    for py in sorted(_PACKAGE.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "scripts" or mod.startswith("scripts."):
                    found.append((py, node.lineno, mod))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "scripts" or alias.name.startswith("scripts."):
                        found.append((py, node.lineno, alias.name))
    return found


def test_the_package_does_import_scripts_at_all():
    """Guard the guard: if these imports ever disappear, this file should
    stop claiming to protect something."""
    assert _script_imports(), (
        "no `from scripts.*` imports found -- either the coupling was removed "
        "(delete this test) or the AST walk is broken (fix it)"
    )


@pytest.mark.parametrize(
    "source,lineno,module",
    _script_imports(),
    ids=lambda v: v.name if isinstance(v, Path) else str(v),
)
def test_every_imported_script_module_exists(source, lineno, module):
    target = _ROOT / Path(*module.split(".")).with_suffix(".py")
    package_dir = _ROOT / Path(*module.split("."))
    assert target.is_file() or (package_dir / "__init__.py").is_file(), (
        f"{source.relative_to(_ROOT)}:{lineno} imports {module!r}, but neither "
        f"{target.relative_to(_ROOT)} nor {package_dir.relative_to(_ROOT)}/__init__.py "
        f"exists. The command that reaches this line raises ModuleNotFoundError "
        f"at runtime while --help still advertises it."
    )
