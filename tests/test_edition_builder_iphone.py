"""The iPhone edition, built from the same builder as Car.

Grey's original instruction, from the 2026-08-31 handoff: "It's an
Edition, not a utility. It should inherit the console's Build-an-Edition
selection, dry-run and verification rather than becoming a fourth
standalone script with its own spelling of --execute."

So it is not a port of ORPHEUS's build_aac_port_iphone.py. That script
selects from ALREADY-ENCODED output and copies a subset; here the format
is identical to Car (AAC 256k, -14 LUFS, <=48 kHz) and only the selection
differs, so a second encoder would be a second thing to keep correct. The
three sample-rate defects found in the Car path in one day are the
argument against having two copies of that path.

Two real differences from Car:
  - a size budget, which for iPhone is not optional: 81.7 GB of library
    does not fit a 30 GB phone
  - no masking. Cabin noise exists to sit under road noise; on headphones
    it is just noise mixed into the music.
"""

from __future__ import annotations

import sqlite3

import pytest

from musaeus.editions import CAR, EDITIONS, IPHONE, select_edition


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE archive (
        file_path TEXT PRIMARY KEY, artist TEXT, album TEXT, title TEXT,
        genre TEXT, duration REAL, size_bytes INTEGER, status TEXT)""")
    for i in range(40):
        c.execute("INSERT INTO archive VALUES (?,?,?,?,?,?,?,?)",
                  (f"/m/{i:03d}.m4a", "A", "Al", f"T{i}",
                   ["Rock", "Jazz", "Blues"][i % 3], 240.0, 40_000_000, "CATALOGUED"))
    return c


class TestSameFormatAsCar:
    def test_iphone_and_car_are_format_identical(self) -> None:
        """If these ever diverge, the shared encoder stops being correct
        for one of them -- which is the whole reason there is only one."""
        assert IPHONE.codec == CAR.codec
        assert IPHONE.bitrate_kbps == CAR.bitrate_kbps
        assert IPHONE.lufs_target == CAR.lufs_target
        assert IPHONE.max_sample_rate == CAR.max_sample_rate

    def test_both_are_reachable_by_name(self) -> None:
        assert EDITIONS["iphone"] is IPHONE
        assert EDITIONS["car"] is CAR


class TestBudget:
    def test_a_budget_actually_constrains_the_selection(self, conn) -> None:
        full = select_edition(conn, IPHONE)
        limited = select_edition(conn, IPHONE, budget_bytes=30_000_000)
        assert len(limited.included) < len(full.included)
        assert limited.estimated_bytes <= 30_000_000

    def test_nothing_is_lost_only_deferred(self, conn) -> None:
        """Every track is accounted for -- included or reported as skipped,
        never silently dropped."""
        sel = select_edition(conn, IPHONE, budget_bytes=30_000_000)
        assert len(sel.included) + len(sel.skipped_for_budget) == 40

    def test_car_without_a_budget_takes_everything(self, conn) -> None:
        """Car needs no budget: 81.7 GB against a 250 GB stick."""
        sel = select_edition(conn, CAR)
        assert len(sel.included) == 40
        assert sel.skipped_for_budget == []

    def test_a_budget_smaller_than_one_track_yields_an_empty_edition(self, conn) -> None:
        """It must report an empty selection, not crash and not part-fill."""
        sel = select_edition(conn, IPHONE, budget_bytes=1_000)
        assert sel.included == []
        assert len(sel.skipped_for_budget) == 40

    def test_selection_is_stable_across_runs(self, conn) -> None:
        """A phone re-synced from a rebuild must not silently differ."""
        a = [t.file_path for t in select_edition(conn, IPHONE, budget_bytes=30_000_000).included]
        b = [t.file_path for t in select_edition(conn, IPHONE, budget_bytes=30_000_000).included]
        assert a == b and a
