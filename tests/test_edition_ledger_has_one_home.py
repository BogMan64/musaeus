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


def test_the_car_count_is_the_records_else_the_old_builders(tmp_path):
    # Cloud review of #53: `musaeus status` and the console each wrote this.
    import sqlite3
    from types import SimpleNamespace

    from musaeus.edition_ledger import Copy, car_copy_count, open_ledger, record

    cfg = SimpleNamespace(db_history_dir=tmp_path / "hist")
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE archive (car_export_path TEXT)")
    conn.executemany("INSERT INTO archive VALUES (?)", [("/car/a",), ("/car/b",), (None,)])
    assert car_copy_count(cfg, conn) == 2  # built the old way
    led = open_ledger(tmp_path / "hist" / "editions.db")
    record(led, Copy("car", "h", "m", 1, "/car/x", "now", -14.0, "linear"))
    led.close()
    assert car_copy_count(cfg, conn) == 1  # the edition framework's record wins


def test_a_record_from_before_measurements_reads_as_nothing_measured(tmp_path):
    # The vault's editions.db predates the measurements table; a read-only
    # open (dry runs, the doctor, the console) cannot create it.
    import sqlite3

    from musaeus.edition_ledger import measured_hashes, measurements_of, open_for_reading

    path = tmp_path / "editions.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE edition_copies (edition TEXT, master_hash TEXT)")
    old.commit()
    old.close()
    ro = open_for_reading(path)
    assert measurements_of(ro, "h") == {} and measured_hashes(ro, "aac") == set()
    ro.close()


def test_the_report_counts_car_copies_as_status_does(tmp_path):
    # Cloud review of #53: `musaeus report` still counted car_export_path, so
    # after a new-style build it said 'Car export 0' while status said N.
    import importlib.util
    from types import SimpleNamespace

    from musaeus.edition_ledger import Copy, open_ledger, record

    spec = importlib.util.spec_from_file_location("mreport", ROOT / "scripts" / "musaeus_report.py")
    mreport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mreport)
    cfg = SimpleNamespace(db_history_dir=tmp_path / "hist")
    led = open_ledger(tmp_path / "hist" / "editions.db")
    record(led, Copy("car", "h", "m", 1, "/car/x", "now", -14.0, "linear"))
    led.close()
    from musaeus.db import open_db

    conn = open_db(tmp_path / "musaeus.db")  # the real schema, empty
    data = mreport._gather(conn, cfg)
    assert data["car_export"] == 1


def test_only_edition_ledger_counts_the_car_copies():
    offenders = [
        str(p.relative_to(ROOT))
        for p in [*(ROOT / "musaeus").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]
        if p != HOME
        and "COUNT(*) FROM archive WHERE car_export_path" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], offenders
