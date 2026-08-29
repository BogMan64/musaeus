"""
MUSAEUS — BaseStage.execute() error handling.

execute() is the single path every one of the 30+ stages runs through, and
the only thing standing between a stage that raises and a pipeline that dies
mid-batch. It had no dedicated test: stage-specific suites exercise it
incidentally on their happy paths, but nothing covered validation failure,
StageError, an unexpected exception, or the failure-report contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext, StageResult
from musaeus.db import open_db
from musaeus.stages.base import BaseStage, StageError


@pytest.fixture
def cfg(tmp_path) -> MusicConfig:
    return MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )


@pytest.fixture
def ctx(cfg: MusicConfig) -> RunContext:
    cfg.ensure_dirs()
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


@pytest.fixture
def ctx_dry(cfg: MusicConfig) -> RunContext:
    cfg.ensure_dirs()
    open_db(cfg.db_path).close()
    return RunContext.new(cfg, open_db(cfg.db_path, read_only=True), dry_run=True)


class _Ok(BaseStage):
    NAME = "ok_stage"

    def validate(self, ctx: RunContext) -> None:
        pass

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)
        result.files_processed = 3
        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=True)
        result.notes.append("preview")
        ctx.record_stage(result)
        return result


class _ValidateFails(_Ok):
    NAME = "validate_fails"

    def validate(self, ctx: RunContext) -> None:
        raise StageError("prerequisite missing")


class _RunRaisesStageError(_Ok):
    NAME = "raises_stage_error"

    def run(self, ctx: RunContext) -> StageResult:
        raise StageError("cannot proceed")


class _RunRaisesUnexpected(_Ok):
    NAME = "raises_unexpected"

    def run(self, ctx: RunContext) -> StageResult:
        raise ZeroDivisionError("boom")


class _DryRunRaisesUnexpected(_Ok):
    """execute() dispatches to dry_run() under a preview, so a stage whose
    run() raises is not enough to exercise the preview failure path."""

    NAME = "dry_raises_unexpected"

    def dry_run(self, ctx: RunContext) -> StageResult:
        raise ZeroDivisionError("boom")


class TestHappyPath:
    def test_run_is_dispatched_when_not_dry(self, ctx):
        result = _Ok().execute(ctx)
        assert result.success
        assert result.dry_run is False
        assert result.files_processed == 3

    def test_dry_run_is_dispatched_when_dry(self, ctx_dry):
        result = _Ok().execute(ctx_dry)
        assert result.success
        assert result.dry_run is True
        assert "preview" in result.notes


class TestFailuresAreContainedNotRaised:
    """A failing stage must become a reported result, never an exception --
    otherwise one bad stage aborts the whole pipeline mid-batch."""

    @pytest.mark.parametrize(
        "stage_cls, fragment",
        [
            (_ValidateFails, "prerequisite missing"),
            (_RunRaisesStageError, "cannot proceed"),
            (_RunRaisesUnexpected, "boom"),
        ],
        ids=["validate", "stage_error", "unexpected"],
    )
    def test_failure_is_returned_as_a_result(self, ctx, stage_cls, fragment):
        result = stage_cls().execute(ctx)
        assert result.success is False
        assert any(fragment in e for e in result.errors)

    def test_validation_failure_is_labelled_as_such(self, ctx):
        result = _ValidateFails().execute(ctx)
        assert any(e.startswith("ValidationError:") for e in result.errors)

    def test_unexpected_exception_is_labelled_as_such(self, ctx):
        result = _RunRaisesUnexpected().execute(ctx)
        assert any(e.startswith("Unexpected:") for e in result.errors)

    def test_a_failed_stage_is_still_recorded_on_the_context(self, ctx):
        _RunRaisesStageError().execute(ctx)
        assert [r.stage_name for r in ctx.stage_results] == ["raises_stage_error"]


class TestFailureReport:
    def test_report_is_written_and_referenced(self, ctx):
        result = _RunRaisesUnexpected().execute(ctx)

        refs = [e for e in result.errors if e.startswith("Failure report: ")]
        assert refs, "the result must point at the report it wrote"
        path = refs[0].removeprefix("Failure report: ")
        report = json.loads(Path(path).read_text(encoding="utf-8"))

        assert report["stage"] == "raises_unexpected"
        assert report["phase"] == "run"
        assert report["run_id"] == ctx.run_id
        assert report["exception_type"] == "ZeroDivisionError"
        assert "boom" in report["exception_message"]
        assert "ZeroDivisionError" in report["traceback"]

    def test_report_records_the_last_item_the_stage_was_working_on(self, ctx):
        ctx.set("_current_item", "/vault/INBOX/troublesome.flac")
        result = _RunRaisesUnexpected().execute(ctx)
        path = next(
            e.removeprefix("Failure report: ")
            for e in result.errors
            if e.startswith("Failure report: ")
        )
        assert json.loads(Path(path).read_text(encoding="utf-8"))["last_item"] == (
            "/vault/INBOX/troublesome.flac"
        )

    def test_validation_failure_reports_the_validate_phase(self, ctx):
        result = _ValidateFails().execute(ctx)
        path = next(
            e.removeprefix("Failure report: ")
            for e in result.errors
            if e.startswith("Failure report: ")
        )
        assert json.loads(Path(path).read_text(encoding="utf-8"))["phase"] == "validate"

    def test_no_report_is_written_under_dry_run(self, ctx_dry):
        """A preview must not leave artifacts behind (nor mkdir RUNS/FAILURES
        to hold them) -- the error still reaches result.errors."""
        result = _DryRunRaisesUnexpected().execute(ctx_dry)

        assert result.success is False
        assert any("boom" in e for e in result.errors)
        assert not any(e.startswith("Failure report: ") for e in result.errors)
        assert not (ctx_dry.runs_root / "FAILURES").exists()

    def test_a_broken_reporter_never_masks_the_real_error(self, ctx, monkeypatch):
        """The reporter is best-effort by design: if it cannot write, the
        original failure must still be reported, not replaced."""

        def _explode(self, ctx):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(BaseStage, "_failure_report_path", _explode)

        result = _RunRaisesUnexpected().execute(ctx)

        assert result.success is False
        assert any("boom" in e for e in result.errors)
        assert not any(e.startswith("Failure report: ") for e in result.errors)
