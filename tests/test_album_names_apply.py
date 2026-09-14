"""Applying a proposal must never overwrite an album that is already set.

The version that ran on 2026-09-13 selected rows on confidence alone and
updated whatever archive_id the CSV named, with no check on the current
value. It was safe only because every row in that particular CSV had a blank
album. Re-run the same CSV after a later pass had filled some in and it would
have replaced real album names with proposals -- and the rows it is least
entitled to touch are exactly the ones a human has already ruled on.

A proposal answers "what should go in this empty field". It is not a
correction and it does not outrank anything already there.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "album_names"))

from apply_album_names import apply_rows  # noqa: E402


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE archive (id INTEGER PRIMARY KEY, album TEXT, file_path TEXT)")
    c.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, event_type TEXT, file_path TEXT, "
        "old_value TEXT, new_value TEXT, note TEXT)"
    )
    c.executemany(
        "INSERT INTO archive (id, album, file_path) VALUES (?,?,?)",
        [(1, "", "/a.m4a"), (2, "Abbey Road", "/b.m4a"), (3, None, "/c.m4a")],
    )
    c.commit()
    return c


def _row(archive_id, album, confidence="1-AGREED"):
    return {
        "confidence": confidence,
        "archive_id": str(archive_id),
        "proposed_album": album,
        "file_path": f"/{archive_id}.m4a",
        "note": "",
    }


def _album(conn, rid):
    return conn.execute("SELECT album FROM archive WHERE id=?", (rid,)).fetchone()[0]


class TestItNeverOverwrites:
    def test_a_row_with_an_album_is_skipped_not_replaced(self, conn):
        tally = apply_rows(conn, [_row(2, "Some Compilation")], ("1-AGREED",), live=True)
        assert _album(conn, 2) == "Abbey Road", "an existing album must survive"
        assert tally["skipped_album_already_set"] == 1
        assert tally["written"] == 0

    @pytest.mark.parametrize("rid", [1, 3])
    def test_a_blank_or_null_album_is_filled(self, conn, rid):
        apply_rows(conn, [_row(rid, "Revolver")], ("1-AGREED",), live=True)
        assert _album(conn, rid) == "Revolver"

    def test_the_skip_is_counted_not_silent(self, conn):
        """A silent skip and a silent overwrite look identical in a summary."""
        rows = [_row(1, "Revolver"), _row(2, "Wrong")]
        tally = apply_rows(conn, rows, ("1-AGREED",), live=True)
        assert tally["written"] == 1
        assert tally["skipped_album_already_set"] == 1


class TestDryRunIsReallyDry:
    def test_nothing_is_written_without_live(self, conn):
        tally = apply_rows(conn, [_row(1, "Revolver")], ("1-AGREED",), live=False)
        assert _album(conn, 1) == ""
        assert tally["written"] == 1, "a dry run still reports what it would do"

    def test_a_dry_run_logs_no_events(self, conn):
        apply_rows(conn, [_row(1, "Revolver")], ("1-AGREED",), live=False)
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


class TestTierSelection:
    def test_only_the_named_tiers_are_applied(self, conn):
        rows = [_row(1, "Revolver", "3-SOURCES DISAGREE")]
        tally = apply_rows(conn, rows, ("1-AGREED",), live=True)
        assert _album(conn, 1) == ""
        assert tally["skipped_wrong_tier"] == 1

    def test_a_lower_tier_can_be_asked_for_explicitly(self, conn):
        rows = [_row(1, "Revolver", "2-DEEZER ONLY")]
        apply_rows(conn, rows, ("1-AGREED", "2-DEEZER ONLY"), live=True)
        assert _album(conn, 1) == "Revolver"


class TestItLeavesAnAuditTrail:
    def test_a_write_is_recorded_as_an_event(self, conn):
        apply_rows(conn, [_row(1, "Revolver")], ("1-AGREED",), live=True)
        ev = conn.execute(
            "SELECT event_type, old_value, new_value FROM events"
        ).fetchone()
        assert ev == ("ALBUM_NAME_APPLIED", "", "Revolver")

    def test_a_missing_events_table_is_not_fatal(self, conn):
        """A vault that predates the events table must still be applyable."""
        conn.execute("DROP TABLE events")
        conn.commit()
        apply_rows(conn, [_row(1, "Revolver")], ("1-AGREED",), live=True)
        assert _album(conn, 1) == "Revolver"


class TestRowsThatDoNotLineUp:
    def test_an_unknown_archive_id_is_counted_not_crashed(self, conn):
        tally = apply_rows(conn, [_row(999, "Revolver")], ("1-AGREED",), live=True)
        assert tally["row_not_found"] == 1
        assert tally["written"] == 0

    def test_an_empty_proposal_is_never_written(self, conn):
        tally = apply_rows(conn, [_row(1, "")], ("1-AGREED",), live=True)
        assert _album(conn, 1) == ""
        assert tally["skipped_no_proposal"] == 1
