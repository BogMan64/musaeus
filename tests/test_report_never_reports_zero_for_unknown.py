"""musaeus_report's duplicate counts: right column, and no silent zero.

Two defects lived here together until 2026-09-09, and neither was visible
from the report's output.

  1. Two of the three queries asked for a column named `type`. The
     duplicates table has `duplicate_type`, and always did.

  2. All three queries sat inside one bare `except Exception` that set all
     three counts to 0. The first query SUCCEEDED; the second raised; the
     zero from the handler overwrote the real answer from the first.

The visible result was a report that printed nothing at all about
duplicates while the live database held 2,862 pending groups -- because
the renderer only prints the line `if d["dupe_pending"]`, and zero is
falsy. A wrong column name was being rendered as good news.

That is the failure mode worth a test: not "the count is wrong" but "the
count could not be taken, and the report said zero anyway". Zero is the
answer that makes a reader stop looking, so a count that cannot be taken
has to say so.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from musaeus.db import open_db

_SPEC = importlib.util.spec_from_file_location(
    "musaeus_report", Path(__file__).resolve().parent.parent / "scripts" / "musaeus_report.py"
)
report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report)


@pytest.fixture
def db_with_pending(tmp_path) -> sqlite3.Connection:
    conn = open_db(tmp_path / "musaeus.db")
    rows = [
        ("grp-1", "/x/a.m4a", "EXACT"),
        ("grp-1", "/x/b.m4a", "EXACT"),
        ("grp-2", "/x/c.m4a", "NEAR"),
        ("grp-2", "/x/d.m4a", "NEAR"),
        ("grp-3", "/x/e.m4a", "NEAR"),
        ("grp-3", "/x/f.m4a", "NEAR"),
    ]
    for group_id, file_path, dtype in rows:
        conn.execute(
            "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, "
            "status, run_id, staged_at) VALUES (?,?,?,?,?,?,?)",
            (group_id, file_path, dtype, 1.0, "pending", "run-1", "2026-09-01T00:00:00Z"),
        )
    conn.commit()
    return conn


def test_pending_duplicates_are_counted_not_swallowed(db_with_pending) -> None:
    """Three groups pending, one exact and two near.

    Under the old code every one of these read 0: the `type` query raised
    and the shared handler zeroed the two that had worked.
    """
    d = report._gather(db_with_pending)
    assert d["dupe_pending"] == 3
    assert d["dupe_exact"] == 1
    assert d["dupe_near"] == 2


def test_the_report_actually_prints_the_warning(db_with_pending, capsys, tmp_path) -> None:
    """The count reaching the dict is not enough -- the renderer drops the
    line when the count is falsy, which is how a zero hid 2,862 groups."""
    d = report._gather(db_with_pending)
    report._print_report(d, _FakeConfig(tmp_path))
    out = capsys.readouterr().out
    assert "dupe group(s) pending" in out
    assert "3 dupe group(s) pending" in out


def test_a_count_that_cannot_be_taken_is_unknown_not_zero(tmp_path, capsys) -> None:
    """The whole point. A database whose duplicates table cannot answer
    must produce "unavailable", never a confident zero."""
    conn = open_db(tmp_path / "musaeus.db")
    conn.execute("DROP TABLE duplicates")
    conn.commit()

    d = report._gather(conn)
    assert d["dupe_pending"] is None, "a failed count must be None, not 0"

    report._print_report(d, _FakeConfig(tmp_path))
    out = capsys.readouterr().out
    assert "unavailable" in out
    assert "NOT zero pending" in out


def test_one_broken_query_does_not_zero_the_others(tmp_path) -> None:
    """The specific 2026-09-09 defect: the counts are taken independently,
    so a column missing for one does not erase the answers to the rest."""
    conn = open_db(tmp_path / "musaeus.db")
    conn.execute(
        "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, "
        "status, run_id, staged_at) VALUES (?,?,?,?,?,?,?)",
        ("grp-1", "/x/a.m4a", None, 1.0, "pending", "run-1", "2026-09-01T00:00:00Z"),
    )
    conn.commit()
    d = report._gather(conn)
    # duplicate_type IS NULL, so the EXACT/NEAR counts are legitimately 0
    # while the overall pending count is 1. The overall count must survive.
    assert d["dupe_pending"] == 1
    assert d["dupe_exact"] == 0
    assert d["dupe_near"] == 0


class _FakeConfig:
    def __init__(self, root: Path) -> None:
        self.vault_root = root
        self.inbox = root / "INBOX"
        self.alac_library = root / "ALAC-Library"
        self.db_path = root / "musaeus.db"
