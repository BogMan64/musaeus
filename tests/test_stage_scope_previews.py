"""`corrupt` and `bitrot` must answer "how much work is this?" before you commit hours.

Both stages take hours over the whole library, and neither could be previewed:
``--dry-run`` printed ``no preview available for this stage``. That is not a
wrong answer — it says plainly that it cannot preview — but it reads as
"nothing to do" at the end of a long day, and it already cost a wasted
overnight ``musaeus corrupt --dry-run`` that returned in zero seconds having
done nothing.

The fix is a ``plan_candidates(conn, cfg)`` classmethod: a pure, read-only
count the planner already calls where one exists. It is deliberately *not*
wiring each stage's own ``dry_run()`` into the CLI — that would undo the
guarantee that preview never instantiates a stage, never opens a writable
connection and never calls ``ensure_dirs()``.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from musaeus.planner import build_plan
from musaeus.stages.bitrot import BitRotStage
from musaeus.stages.corrupt import CorruptStage


@pytest.fixture
def cfg(tmp_path):
    archive = tmp_path / "ALAC_Archive"
    archive.mkdir()
    db = tmp_path / "musaeus.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE archive (file_path TEXT, status TEXT)")
    conn.executemany(
        "INSERT INTO archive VALUES (?,?)",
        [
            ("/a.m4a", "CATALOGUED"),
            ("/b.m4a", "CATALOGUED"),
            ("/c.m4a", "DELETED"),  # not scanned
            (None, "CATALOGUED"),  # CATALOGUED but no path: _scan skips it
        ],
    )
    conn.commit()
    conn.close()
    return SimpleNamespace(
        vault_root=tmp_path,
        db_path=db,
        alac_library=tmp_path / "lib",
        alac_archive=archive,
    )


def _ro(cfg):
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class TestCorruptPreview:
    def test_it_counts_what_the_stage_actually_scans(self, cfg):
        """_scan's WHERE clause, not validate's.

        validate counts every CATALOGUED row; _scan additionally requires a
        non-NULL file_path. A preview that used validate's count would
        overstate by every pathless row — two numbers describing one stage and
        disagreeing, which is the failure shape this codebase keeps finding.
        """
        conn = _ro(cfg)
        try:
            n, desc = CorruptStage.plan_candidates(conn, cfg)
        finally:
            conn.close()

        assert n == 2, "the DELETED row and the NULL-path row must both be excluded"
        assert "CATALOGUED" in desc

    def test_it_names_the_decode_budget(self, cfg):
        """The per-run decode cap is what makes the runtime predictable, so it
        belongs in the answer to "how long will this take"."""
        conn = _ro(cfg)
        try:
            _, desc = CorruptStage.plan_candidates(conn, cfg)
        finally:
            conn.close()

        assert str(CorruptStage.NEW_ARRIVAL_DECODE_BUDGET) in desc

    def test_it_is_reachable_through_the_planner(self, cfg):
        """Reachability, not just correctness: a preview nothing calls is the
        `library files with no row: 0` failure in a new place."""
        plan = build_plan(cfg, [CorruptStage])

        assert plan.stages[0].candidates == 2
        assert plan.stages[0].previewable
        assert "no preview available" not in plan.stages[0].description


class TestBitRotPreview:
    def _seed(self, cfg, names, baselined=()):
        for name in names:
            (cfg.alac_archive / name).write_bytes(b"\0")
        conn = sqlite3.connect(cfg.db_path)
        conn.execute("CREATE TABLE archive_tier_hashes (path TEXT PRIMARY KEY)")
        conn.executemany(
            "INSERT INTO archive_tier_hashes VALUES (?)",
            [(str(cfg.alac_archive / n),) for n in baselined],
        )
        conn.commit()
        conn.close()

    def test_the_headline_number_is_uncovered_files_not_a_green_tick(self, cfg):
        """The number that matters is how many files have no baseline.

        A file with no baseline is reported by verify as *new*, not as corrupt.
        So an unbaselined archive yields a green verify that compared nothing —
        measured on 2026-09-08 as a baseline that was 0% valid while reporting
        clean. Anyone previewing this stage is deciding whether to spend hours
        hashing; that coverage figure is the answer they need.
        """
        self._seed(cfg, ["a.m4a", "b.m4a", "c.m4a"], baselined=["a.m4a"])

        conn = _ro(cfg)
        try:
            n, desc = BitRotStage.plan_candidates(conn, cfg)
        finally:
            conn.close()

        assert n == 3
        assert "2 have no baseline" in desc
        assert "33.3% covered" in desc

    def test_no_baseline_table_says_so_rather_than_reporting_zero(self, cfg):
        """Before any baseline exists there is no table. Reporting 0 candidates
        would read as "nothing to do" when the truth is "nothing is protected"."""
        (cfg.alac_archive / "a.m4a").write_bytes(b"\0")

        conn = _ro(cfg)
        try:
            n, desc = BitRotStage.plan_candidates(conn, cfg)
        finally:
            conn.close()

        assert n == 1
        assert "no baseline table" in desc

    def test_an_empty_archive_is_honest(self, cfg):
        conn = _ro(cfg)
        try:
            n, desc = BitRotStage.plan_candidates(conn, cfg)
        finally:
            conn.close()

        assert n == 0
        assert "no audio files" in desc

    def test_it_is_reachable_through_the_planner(self, cfg):
        self._seed(cfg, ["a.m4a"], baselined=["a.m4a"])
        plan = build_plan(cfg, [BitRotStage])

        assert plan.stages[0].candidates == 1
        assert plan.stages[0].previewable
        assert "no preview available" not in plan.stages[0].description
