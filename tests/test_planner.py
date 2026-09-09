"""
Tests for typed run modes and the pure preview planner (P0-04/P0-05).

What went wrong before: `--dry-run` meant "execute with a flag set". Every
dry_run=True call still ran cfg.ensure_dirs() and RunContext.new(), so it
created the real vault skeleton and the real database, and committed
RUN_START/STAGE_COMPLETE events, before any stage ran. P0-02 refused
--dry-run outright rather than let that continue.

The fix is not a better-behaved execution -- it is a different kind of
thing. These tests exist to prove that difference structurally, not to
take it on trust:

  * the planner never instantiates a stage (a stage OBJECT is the
    mutation-capable thing);
  * it never creates a database, even when none exists;
  * before/after state is byte-identical, including directory tree.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from musaeus.planner import (
    SAFETY_STATEMENT,
    Plan,
    PreviewConflict,
    RunMode,
    StagePlan,
    build_plan,
    reject_persistence_flags,
)


class TestRunMode:
    def test_dry_run_and_preview_both_mean_preview(self):
        assert RunMode.resolve(dry_run=True) is RunMode.PREVIEW
        assert RunMode.resolve(preview=True) is RunMode.PREVIEW

    def test_default_is_execute(self):
        assert RunMode.resolve() is RunMode.EXECUTE

    def test_preview_is_not_execute(self):
        """A distinct type, not a boolean on the same path."""
        assert RunMode.PREVIEW is not RunMode.EXECUTE


class TestPersistenceFlagRejection:
    def test_preview_plus_force_is_refused(self):
        with pytest.raises(PreviewConflict, match="--force"):
            reject_persistence_flags(RunMode.PREVIEW, SimpleNamespace(force=True))

    def test_the_offending_flag_is_named(self):
        """A refusal the user cannot act on is only half a refusal."""
        with pytest.raises(PreviewConflict, match="--consolidate"):
            reject_persistence_flags(RunMode.PREVIEW, SimpleNamespace(consolidate=True))

    def test_execute_mode_allows_persistence_flags(self):
        reject_persistence_flags(RunMode.EXECUTE, SimpleNamespace(force=True, apply=True))

    def test_preview_without_persistence_flags_is_fine(self):
        reject_persistence_flags(RunMode.PREVIEW, SimpleNamespace(force=False, json=True))


def _snapshot(root: Path) -> tuple[list[str], str]:
    """Directory tree plus a hash of every file's bytes."""
    entries = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.name.encode())
            h.update(p.read_bytes())
    return entries, h.hexdigest()


class _Stage:
    NAME = "fake"

    def __init__(self):  # pragma: no cover - must never be reached
        raise AssertionError("the planner must never instantiate a stage")

    @classmethod
    def plan_candidates(cls, conn, cfg=None):
        n = conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
        return n, "rows this stage would touch"


class _Unplannable:
    NAME = "opaque"

    def __init__(self):  # pragma: no cover
        raise AssertionError("the planner must never instantiate a stage")


@pytest.fixture
def cfg(tmp_path: Path):
    db = tmp_path / "musaeus.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE archive (file_path TEXT, status TEXT)")
    conn.executemany(
        "INSERT INTO archive VALUES (?,?)", [("/a", "CATALOGUED"), ("/b", "CATALOGUED")]
    )
    conn.commit()
    conn.close()
    return SimpleNamespace(vault_root=tmp_path, db_path=db, alac_library=tmp_path / "lib")


class TestPurePlanner:
    def test_the_planner_never_instantiates_a_stage(self, cfg):
        """Both fakes raise from __init__. Reaching one fails the test.

        This is the structural guarantee: preview has no execution
        authority because it never builds the object that would carry it.
        """
        plan = build_plan(cfg, [_Stage, _Unplannable])
        assert plan.stages[0].candidates == 2

    def test_an_unplannable_stage_is_a_dash_not_a_zero(self, cfg):
        """An unknown reported as zero is how a preview starts lying."""
        plan = build_plan(cfg, [_Unplannable])
        assert plan.stages[0].candidates is None
        assert "—" in plan.render()

    def test_a_broken_stage_planner_does_not_fake_a_zero(self, cfg):
        class Broken(_Unplannable):
            NAME = "broken"

            @classmethod
            def plan_candidates(cls, conn):
                raise RuntimeError("bad query")

        plan = build_plan(cfg, [Broken])
        assert plan.stages[0].candidates is None
        assert "preview failed" in plan.stages[0].description

    def test_preview_changes_nothing_on_disk(self, cfg):
        before = _snapshot(cfg.vault_root)
        build_plan(cfg, [_Stage, _Unplannable])
        assert _snapshot(cfg.vault_root) == before

    def test_preview_never_creates_a_database(self, tmp_path):
        """The original defect: dry-run created the DB before any stage ran."""
        empty = SimpleNamespace(
            vault_root=tmp_path,
            db_path=tmp_path / "musaeus.db",
            alac_library=tmp_path / "lib",
        )
        plan = build_plan(empty, [_Stage])
        assert not (tmp_path / "musaeus.db").exists()
        assert any("no database" in n for n in plan.notes)

    def test_preview_never_creates_the_vault_skeleton(self, tmp_path):
        empty = SimpleNamespace(
            vault_root=tmp_path,
            db_path=tmp_path / "musaeus.db",
            alac_library=tmp_path / "lib",
        )
        build_plan(empty, [_Stage])
        assert list(tmp_path.iterdir()) == []

    def test_the_plan_is_deterministic(self, cfg):
        assert build_plan(cfg, [_Stage]).to_json() == build_plan(cfg, [_Stage]).to_json()

    def test_output_carries_the_safety_statement(self, cfg):
        plan = build_plan(cfg, [_Stage])
        assert SAFETY_STATEMENT in plan.render()
        assert SAFETY_STATEMENT in plan.to_json()

    def test_json_render_is_machine_readable(self, cfg):
        import json

        data = json.loads(build_plan(cfg, [_Stage]).to_json())
        assert data["mode"] == "preview"
        assert data["stages"][0]["candidates"] == 2

    def test_totals_ignore_unplannable_stages(self):
        p = Plan(mode=RunMode.PREVIEW, vault_root=Path("/x"))
        p.stages = [StagePlan("a", 5, ""), StagePlan("b", None, "")]
        assert p.total_candidates == 5


class TestIngestPlanCountsTheInboxNotJustRows:
    """Found by the batch-0 dry run on 2026-08-23.

    The plan reported "ingest 0 pending files" while 20 files sat in
    INBOX waiting. IngestStage.plan_candidates counted archive rows with
    status='PENDING', but a file that has never been scanned has no row
    yet -- so the count was structurally incapable of seeing the very
    thing a run is about to do.

    For the first stage of a pipeline, that is the worst number to get
    wrong: it says "nothing to do" at precisely the moment there is.
    """

    def test_files_in_the_inbox_are_counted(self, tmp_path):
        import sqlite3
        from types import SimpleNamespace

        from musaeus.stages.ingest import IngestStage

        inbox = tmp_path / "INBOX"
        (inbox / "sub").mkdir(parents=True)
        (inbox / "a.m4a").write_bytes(b"x")
        (inbox / "sub" / "b.m4a").write_bytes(b"x")
        (inbox / "notes.txt").write_bytes(b"x")  # non-audio must not count

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE archive (file_path TEXT, status TEXT)")
        conn.commit()

        count, desc = IngestStage.plan_candidates(conn, SimpleNamespace(inbox=inbox))
        assert count == 2, desc
        assert "INBOX" in desc

    def test_an_empty_inbox_reports_zero_honestly(self, tmp_path):
        import sqlite3
        from types import SimpleNamespace

        from musaeus.stages.ingest import IngestStage

        inbox = tmp_path / "INBOX"
        inbox.mkdir()
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE archive (file_path TEXT, status TEXT)")
        conn.commit()
        count, _ = IngestStage.plan_candidates(conn, SimpleNamespace(inbox=inbox))
        assert count == 0

    def test_a_missing_inbox_does_not_raise(self, tmp_path):
        """A vault that has never been set up must still be plannable."""
        import sqlite3
        from types import SimpleNamespace

        from musaeus.stages.ingest import IngestStage

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE archive (file_path TEXT, status TEXT)")
        conn.commit()
        count, _ = IngestStage.plan_candidates(conn, SimpleNamespace(inbox=tmp_path / "nope"))
        assert count == 0
