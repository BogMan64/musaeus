"""Review of #88, finding 10 (2026-10-07): the edition ledger's readers caught
every OperationalError to tolerate a record from before a table existed. A
"database is locked" -- another build writing -- read the same: no measurements
(measure everything again), no copy settings (bake everything again), no ledger
at all. Only a missing table or column means "none" now; anything else is raised.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from musaeus import edition_ledger as el


@pytest.fixture
def busy_ledger(tmp_path):
    path = tmp_path / el.LEDGER_FILENAME
    conn = el.open_ledger(path)
    el.keep_measurement(conn, "h1", "car-v1", {"input_i": -20.0})
    conn.close()
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("BEGIN EXCLUSIVE")  # another build in the middle of a write
    yield path
    writer.execute("ROLLBACK")
    writer.close()


def test_a_locked_ledger_is_not_an_empty_one(busy_ledger):
    reader = sqlite3.connect(busy_ledger, timeout=0.1)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        el.measured_hashes(reader, "car")


def test_a_locked_ledger_is_not_a_missing_one(busy_ledger, monkeypatch):
    monkeypatch.setattr(el, "_READ_TIMEOUT_S", 0.1, raising=False)
    config = SimpleNamespace(db_history_dir=busy_ledger.parent)
    monkeypatch.setattr(el, "ledger_path", lambda cfg: busy_ledger)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        el.recorded_copies(config, "car")


def test_a_record_from_before_measurements_still_reads_as_none(tmp_path):
    old = sqlite3.connect(tmp_path / "old.db")
    old.execute("CREATE TABLE edition_copies (x)")
    assert el.measured_hashes(old, "car") == set()
    assert el.measurements_of(old, "h1") == {}
