'''One way to add a column, and a guard that keeps it that way.

Nine modules had written the same eight-line function -- integrity,
mb_enrich, transcode, original_year, identity_tag, acousticid, auditor,
albumart and deep_scan -- each reading PRAGMA table_info, diffing a set,
and ALTERing in a loop. Not one was wrong, which is the point: it is a
shape small enough that writing it again is easier than finding it.

The columns themselves stay with their stage. acousticid's five mean
nothing to albumart, and declaring them next to the code that reads them
is right. Only the mechanism is shared -- the same split as brackets.py
(share the alphabet, not the judgement) and duration.py (share the
reading, not the choice).

The last test here is the one that matters. Every other fix this session
removed duplication that had already happened; this one makes the next
copy fail CI instead of waiting to be noticed.
'''

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from musaeus.db import ensure_columns

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE archive (file_path TEXT)")
    return c


def _cols(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(archive)").fetchall()}


def test_it_adds_what_is_missing_and_reports_how_many(conn) -> None:
    assert ensure_columns(conn, (("a", "TEXT"), ("b", "INTEGER"))) == 2
    assert {"a", "b"} <= _cols(conn)


def test_it_is_idempotent(conn) -> None:
    ensure_columns(conn, (("a", "TEXT"),))
    assert ensure_columns(conn, (("a", "TEXT"),)) == 0


def test_it_adds_only_the_missing_one(conn) -> None:
    ensure_columns(conn, (("a", "TEXT"),))
    assert ensure_columns(conn, (("a", "TEXT"), ("b", "TEXT"))) == 1


def test_a_declaration_with_a_default_survives(conn) -> None:
    """auditor_flagged is "INTEGER DEFAULT 0" -- the decl is passed
    through, not parsed."""
    ensure_columns(conn, (("flagged", "INTEGER DEFAULT 0"),))
    row = next(r for r in conn.execute("PRAGMA table_info(archive)") if r[1] == "flagged")
    assert row[4] == "0"


@pytest.mark.parametrize(
    "module,expected",
    [
        ("musaeus.stages.integrity", {"integrity_ok", "integrity_checked_at"}),
        ("musaeus.stages.mb_enrich", {"mb_artist_id", "mb_enriched_at"}),
        ("musaeus.stages.transcode", {"transcode_path", "transcode_at"}),
        ("musaeus.stages.original_year", {"original_year", "original_year_checked_at"}),
        ("musaeus.stages.acousticid", {"chromaprint", "acousticid_checked_at"}),
        ("musaeus.stages.auditor", {"auditor_lufs", "auditor_flagged"}),
        ("musaeus.stages.albumart", {"has_art", "art_px"}),
        ("musaeus.stages.identity_tag", {"identity_tagged_at"}),
    ],
)
def test_each_stage_still_creates_its_own_columns(conn, module, expected) -> None:
    """Behaviour preserved: the stage still owns and creates its columns."""
    import importlib

    importlib.import_module(module)._ensure_columns(conn)
    assert expected <= _cols(conn)


def test_deep_scan_keeps_its_public_name(conn) -> None:
    """cli.py and corrupt.py both import deep_scan.ensure_columns."""
    from musaeus.deep_scan import ensure_columns as ds

    ds(conn)
    assert {"decode_checked_at", "decode_ok", "decode_errors"} <= _cols(conn)


# ── The guard ─────────────────────────────────────────────────────────────────


def test_only_db_may_alter_a_table() -> None:
    """Fail the tenth copy at CI instead of finding it in an audit.

    A plain grep flags prose. So does a naive AST walk: the first version
    of this test failed on musaeus/state/schema.py, whose module docstring
    discusses "ALTER TABLE ADD COLUMN" while adding no column at all.
    Docstrings and other bare string statements are therefore skipped, and
    only strings in executable positions are considered.
    """
    offenders: list[str] = []
    for path in sorted((_ROOT / "musaeus").rglob("*.py")):
        if path.name == "db.py":
            continue  # the one legitimate home
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:  # pragma: no cover
            continue
        # Bare string statements -- module, class and function docstrings,
        # plus any standalone string -- are prose, not SQL.
        prose = {
            id(n.value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
        }
        for node in ast.walk(tree):
            if id(node) in prose:
                continue
            sql = None
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                sql = node.value
            elif isinstance(node, ast.JoinedStr):
                sql = "".join(
                    v.value for v in node.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
            if sql and "ALTER TABLE" in sql.upper() and "ADD COLUMN" in sql.upper():
                offenders.append(f"{path.relative_to(_ROOT)}:{node.lineno}")
    assert not offenders, (
        "ALTER TABLE ... ADD COLUMN outside db.py — use db.ensure_columns "
        f"instead: {offenders}"
    )
