"""The ingest preview must count what ingest would actually do.

The planner's contract is "items this stage would act on". Ingest's
plan_candidates has now been wrong twice, in opposite directions:

  - counting PENDING rows, which reported 0 while the inbox was full,
    because a file that has never been scanned has no row yet;
  - then `waiting + pending`, which counts files run() will skip AND --
    when those PENDING rows point at files still sitting in INBOX, which
    on 2026-09-01 all 10,489 of them did -- adds them to themselves. The
    preview offered 30,257 for a run that ingested 9,279.

Nothing the second version said was false: there really were 19,768 files
in INBOX and 10,489 PENDING rows. Only the sum meant nothing, and the sum
is the number a person reads before deciding to run it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from musaeus.stages.ingest import IngestStage


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE archive (file_path TEXT PRIMARY KEY, status TEXT)")
    return c


def _cfg(inbox: Path) -> SimpleNamespace:
    return SimpleNamespace(inbox=inbox)


def _files(inbox: Path, n: int, prefix: str = "t") -> list[Path]:
    inbox.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        p = inbox / f"{prefix}{i}.flac"
        p.write_bytes(b"x")
        out.append(p)
    return out


def _row(conn, path: Path, status: str = "PENDING") -> None:
    conn.execute("INSERT INTO archive (file_path, status) VALUES (?,?)", (str(path), status))
    conn.commit()


def test_counts_files_with_no_row(conn, tmp_path: Path) -> None:
    inbox = tmp_path / "INBOX"
    _files(inbox, 5)
    count, desc = IngestStage.plan_candidates(conn, _cfg(inbox))
    assert count == 5
    assert "new files in INBOX" in desc


def test_a_pending_row_for_an_inbox_file_is_not_added_to_the_inbox_count(
    conn, tmp_path: Path
) -> None:
    """The 2026-09-01 bug, in miniature.

    Every one of these files is both 'waiting in INBOX' and a 'PENDING
    row'. Summing the two counts each of them twice and reports work that
    run() will skip entirely.
    """
    inbox = tmp_path / "INBOX"
    files = _files(inbox, 10)
    for p in files[:6]:
        _row(conn, p)  # already ingested, still sitting in INBOX
    count, desc = IngestStage.plan_candidates(conn, _cfg(inbox))
    assert count == 4, "only the 4 files without rows are work"
    assert count != 16, "the old sum double-counted the 6 registered files"
    assert "6 already have rows" in desc


def test_everything_already_ingested_is_zero_work(conn, tmp_path: Path) -> None:
    inbox = tmp_path / "INBOX"
    for p in _files(inbox, 4):
        _row(conn, p)
    count, _ = IngestStage.plan_candidates(conn, _cfg(inbox))
    assert count == 0


def test_a_full_inbox_never_reports_zero(conn, tmp_path: Path) -> None:
    """The FIRST bug: counting rows reported 0 while the inbox was full."""
    inbox = tmp_path / "INBOX"
    _files(inbox, 7)
    count, _ = IngestStage.plan_candidates(conn, _cfg(inbox))
    assert count == 7


def test_pending_rows_outside_inbox_are_not_ingest_s_work(conn, tmp_path: Path) -> None:
    """Sentinel picks these up, not Ingest. They must not inflate the count."""
    inbox = tmp_path / "INBOX"
    _files(inbox, 2)
    _row(conn, tmp_path / "elsewhere" / "gone.flac")
    count, _ = IngestStage.plan_candidates(conn, _cfg(inbox))
    assert count == 2


def test_non_audio_files_are_not_counted(conn, tmp_path: Path) -> None:
    inbox = tmp_path / "INBOX"
    _files(inbox, 3)
    (inbox / "cover.jpg").write_bytes(b"x")
    (inbox / "notes.txt").write_bytes(b"x")
    count, _ = IngestStage.plan_candidates(conn, _cfg(inbox))
    assert count == 3


def test_missing_inbox_is_zero_and_says_so(conn, tmp_path: Path) -> None:
    count, desc = IngestStage.plan_candidates(conn, _cfg(tmp_path / "nope"))
    assert count == 0
    assert "does not exist" in desc
