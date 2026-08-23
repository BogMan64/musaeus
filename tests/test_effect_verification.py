"""
Tests for BaseStage effect verification.

The fault class this exists to catch: in two days, five separate
components were found reporting success while doing nothing at all.
rebuild-db dispatched on event names that did not exist. Forge wrote to
a tag key mutagen accepts but cannot serialise. GenreCanon parsed a
separator its file never used. PermissionsStage swept a directory that
is always empty. A shell helper dropped its arguments.

Every one passed its tests, because the tests asserted that the call was
made rather than that anything changed. Verification runs AFTER run()
and asks the only question that matters: did the claimed effect happen?
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from musaeus.context import StageResult
from musaeus.stages.base import BaseStage


class _Stage(BaseStage):
    NAME = "test-stage"

    def __init__(self, changed: int, problems: list[str] | None = None):
        self._changed = changed
        self._problems = problems or []

    def validate(self, ctx): ...

    def dry_run(self, ctx):
        return self._make_result(dry_run=True)

    def run(self, ctx):
        r = self._make_result(dry_run=False)
        r.files_changed = self._changed
        return r

    def verify_effect(self, ctx, result):
        return list(self._problems)


@pytest.fixture
def ctx():
    return SimpleNamespace(dry_run=False, record_stage=lambda _r: None, conn=None)


def test_a_stage_that_did_what_it_claimed_is_marked_verified(ctx):
    r = _Stage(changed=5).execute(ctx)
    assert r.verified is True
    assert r.success is True
    assert "✓verified" in r.summarise()


def test_a_stage_that_changed_nothing_on_disk_is_flagged(ctx):
    """The whole point: 'changed=12279' with nothing on disk must fail."""
    r = _Stage(changed=12279, problems=["no sampled file has the tag"]).execute(ctx)
    assert r.verified is False
    assert "✗UNVERIFIED" in r.summarise()
    assert any("could not be confirmed" in e for e in r.errors)
    assert r.verify_notes == ["no sampled file has the tag"]


def test_no_changes_means_nothing_to_verify(ctx):
    """A stage with nothing to do is not 'unverified' -- it made no claim."""
    r = _Stage(changed=0).execute(ctx)
    assert r.verified is None
    assert "verified" not in r.summarise()


def test_stages_that_only_report_are_never_marked_unverified(ctx):
    class Reporter(_Stage):
        CLAIMS_EFFECT = False

    r = Reporter(changed=5, problems=["would fail if checked"]).execute(ctx)
    assert r.verified is None


def test_a_broken_verifier_cannot_fail_a_good_run(ctx):
    """Verification is a safety net, not a new way to break the pipeline."""

    class Exploding(_Stage):
        def verify_effect(self, ctx, result):
            raise RuntimeError("verifier bug")

    r = Exploding(changed=5).execute(ctx)
    assert r.success is True
    assert r.verified is None
    assert any("verification errored" in n for n in r.verify_notes)


def test_dry_run_is_never_verified(ctx):
    ctx.dry_run = True
    r = _Stage(changed=5, problems=["should not run"]).execute(ctx)
    assert r.verified is None


def test_default_verify_effect_claims_nothing():
    """Stages that have not opted in must not report a hollow 'verified'."""

    class Bare(BaseStage):
        NAME = "bare"

        def validate(self, ctx): ...
        def dry_run(self, ctx):
            return self._make_result(dry_run=True)

        def run(self, ctx):
            return self._make_result(dry_run=False)

    assert Bare().verify_effect(None, StageResult("bare", True)) == []


class TestStageHooksAreCorrectlyBound:
    """Guards a mistake I made on 2026-08-23 and want caught next time.

    Inserting verify_effect() at the top of two stage classes landed it
    BETWEEN an existing @classmethod decorator and its function. The
    decorator attached to verify_effect (which takes self) and
    plan_candidates silently lost it, so the planner called it with the
    wrong arity.

    It surfaced only because the planner reports "preview failed: ..."
    instead of substituting a zero -- had it defaulted to 0, the plan would
    have quietly under-reported 1,735 pending duplicate groups.
    """

    def test_plan_candidates_is_a_classmethod_everywhere_it_exists(self):
        from musaeus.stages import DEFAULT_PIPELINE, FULL_PIPELINE

        for stage in {*DEFAULT_PIPELINE, *FULL_PIPELINE}:
            fn = stage.__dict__.get("plan_candidates")
            if fn is None:
                continue
            assert isinstance(fn, classmethod), (
                f"{stage.__name__}.plan_candidates must be a @classmethod; "
                "the planner calls it on the class and never instantiates a stage"
            )

    def test_verify_effect_is_an_instance_method_everywhere_it_exists(self):
        from musaeus.stages import DEFAULT_PIPELINE, FULL_PIPELINE

        for stage in {*DEFAULT_PIPELINE, *FULL_PIPELINE}:
            fn = stage.__dict__.get("verify_effect")
            if fn is None:
                continue
            assert not isinstance(fn, classmethod | staticmethod), (
                f"{stage.__name__}.verify_effect takes self and is called on an instance"
            )

    def test_every_plan_candidates_is_callable_with_just_a_connection(self):
        """Arity check: the exact failure that slipped through."""
        import sqlite3

        from musaeus.stages import DEFAULT_PIPELINE

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE archive (artist TEXT, genre TEXT, status TEXT, bpm REAL, "
            "rg_tagged_at TEXT, finalized_at TEXT)"
        )
        conn.execute("CREATE TABLE duplicates (group_id TEXT, status TEXT)")
        conn.commit()
        for stage in DEFAULT_PIPELINE:
            if "plan_candidates" not in stage.__dict__:
                continue
            count, desc = stage.plan_candidates(conn)
            assert isinstance(count, int)
            assert desc
