"""Where the edition record lives is said in one place: edition_ledger.

Cloud review of #49: the delete tool hard-coded _db_backups/editions.db -- a
second definition of the record's home, which would silently find nothing
the day db_history_dir moves (it moved once, 2026-08-21). CLAUDE.md: write a
guard whenever a concept is consolidated. Docstrings are skipped (a
docstring is an ast.Constant too, and prose may name the file).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT / "musaeus" / "edition_ledger.py"


def _docstrings(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def test_only_edition_ledger_names_the_record_file():
    offenders = []
    for path in [*(ROOT / "musaeus").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]:
        if path == HOME:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstrings(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "editions.db" in node.value
                and id(node) not in skip
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, offenders
