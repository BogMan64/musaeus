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


def test_a_plan_reads_the_record_without_creating_or_changing_it(tmp_path):
    # Second review of #49: three callers each built an empty in-memory
    # record from the private _SCHEMA. One helper now: read-only when the
    # record exists, empty (and nothing created) before the first build.
    import sqlite3

    import pytest

    from musaeus.edition_ledger import Copy, copies, open_for_reading, open_ledger, record

    path = tmp_path / "hist" / "editions.db"
    empty = open_for_reading(path)
    assert copies(empty, "car") == {} and not path.exists() and not path.parent.exists()
    empty.close()

    conn = open_ledger(path)
    record(conn, Copy("car", "h", "m", 1, "o", "now", -14.0, "linear"))
    conn.close()
    ro = open_for_reading(path)
    assert set(copies(ro, "car")) == {"h"}
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM edition_copies")
    ro.close()


def test_nothing_outside_edition_ledger_reaches_for_its_schema():
    offenders = [
        str(p.relative_to(ROOT))
        for p in [*(ROOT / "musaeus").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]
        if p != HOME
        and "_SCHEMA" in p.read_text(encoding="utf-8")
        and "edition_ledger" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], offenders
