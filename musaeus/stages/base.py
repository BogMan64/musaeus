#!/usr/bin/env python3
"""
MUSAEUS — Stage base class (ABC protocol).

Every pipeline stage MUST subclass BaseStage and implement:
  run(ctx)      — execute with real side-effects
  dry_run(ctx)  — report what WOULD happen, zero mutations
  validate(ctx) — pre-flight checks, raise StageError if prerequisites fail

Rules:
  - dry_run() is never optional. If a stage has no meaningful dry preview,
    raise NotImplementedError with a clear message explaining why.
  - Stages must NOT commit the DB themselves. Record changes, call
    ctx.record_stage(result) — the context handles commits.
  - Stages must log every file-level change via ctx.log_event().
  - Stages must never exit() or sys.exit(). Raise StageError instead.
  - No module-level side effects (no mkdir, no logging.basicConfig, etc.)
"""

from __future__ import annotations

import json
import logging
import traceback
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from ..context import RunContext, StageResult

logger = logging.getLogger(__name__)


class StageError(Exception):
    """
    Raised by a stage when it cannot proceed.
    The pipeline runner catches this and marks the stage as failed.
    """


class BaseStage(ABC):
    """
    Abstract base for all Musaeus pipeline stages.

    Subclass convention:
        class MyStage(BaseStage):
            NAME = "my_stage"

            def validate(self, ctx: RunContext) -> None:
                if not ctx.inbox.exists():
                    raise StageError(f"Inbox missing: {ctx.inbox}")

            def run(self, ctx: RunContext) -> StageResult:
                result = self._make_result(dry_run=False)
                ...
                ctx.record_stage(result)
                return result

            def dry_run(self, ctx: RunContext) -> StageResult:
                result = self._make_result(dry_run=True)
                ...
                ctx.record_stage(result)
                return result
    """

    #: Override in subclass — used for logging, DB stage field, display names
    NAME: str = "unnamed_stage"

    # ── Abstract interface ────────────────────────────────────────────────────

    @abstractmethod
    def run(self, ctx: RunContext) -> StageResult:
        """Execute the stage. May write files, update DB, move files."""
        ...

    @abstractmethod
    def dry_run(self, ctx: RunContext) -> StageResult:
        """
        Report what run() would do — NO mutations.
        Must be implemented. Never a no-op.
        """
        ...

    @abstractmethod
    def validate(self, ctx: RunContext) -> None:
        """
        Pre-flight checks. Raise StageError if prerequisites are not met.
        Called before run() or dry_run().
        """
        ...

    # ── Helper ────────────────────────────────────────────────────────────────

    def _make_result(self, dry_run: bool = False) -> StageResult:
        """Create a fresh StageResult for this stage."""
        from ..context import StageResult

        return StageResult(stage_name=self.NAME, success=True, dry_run=dry_run)

    def _failure_report_path(self, ctx: RunContext) -> Path:
        """
        Where the next failure report for this stage/run will be written.
        Lives under {runs_root}/FAILURES/ so it survives a DB wipe and is
        trivial to find without grepping logs.
        """
        d = ctx.runs_root / "FAILURES"
        d.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return d / f"{self.NAME}_{ctx.run_id}_{stamp}.json"

    def _write_failure_report(self, ctx: RunContext, exc: Exception, phase: str) -> str:
        """
        Write a structured, self-contained failure report: what stage, what
        phase (validate/run/dry_run), the exception, full traceback, and the
        last item the stage was working on (if the stage recorded one via
        ctx.set("_current_item", ...) before the failure).

        This function must never itself raise — a broken reporter must not
        mask or replace the real error. Returns the report path, or "" if
        writing the report failed for any reason.
        """
        try:
            report_path = self._failure_report_path(ctx)
            report = {
                "stage": self.NAME,
                "phase": phase,  # "validate" | "run" | "dry_run"
                "run_id": ctx.run_id,
                "dry_run": ctx.dry_run,
                "occurred_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
                "traceback": traceback.format_exc(),
                "last_item": ctx.get("_current_item"),
            }
            report_path.write_text(
                json.dumps(report, indent=2, default=str), encoding="utf-8"
            )
            return str(report_path)
        except Exception:  # noqa: BLE001 — reporting failures must never propagate
            logger.exception(
                "[%s] failed to write failure report (secondary error, "
                "original error is still recorded in result.errors)",
                self.NAME,
            )
            return ""

    def execute(self, ctx: RunContext) -> StageResult:
        """
        Public entry point called by the pipeline runner.
        Runs validate(), then run() or dry_run() depending on ctx.dry_run.
        Wraps exceptions into a failed StageResult so the pipeline continues,
        and writes a structured failure report to {runs_root}/FAILURES/ so
        any crash can be diagnosed from one file instead of hunting logs.
        """
        try:
            self.validate(ctx)
        except StageError as exc:
            logger.error("[%s] validation failed: %s", self.NAME, exc)
            result = self._make_result(dry_run=ctx.dry_run)
            result.success = False
            result.errors.append(f"ValidationError: {exc}")
            report_path = self._write_failure_report(ctx, exc, phase="validate")
            if report_path:
                result.errors.append(f"Failure report: {report_path}")
                logger.error("[%s] failure report: %s", self.NAME, report_path)
            ctx.record_stage(result)
            return result

        phase = "dry_run" if ctx.dry_run else "run"
        try:
            if ctx.dry_run:
                return self.dry_run(ctx)
            return self.run(ctx)
        except StageError as exc:
            logger.error("[%s] stage error: %s", self.NAME, exc)
            result = self._make_result(dry_run=ctx.dry_run)
            result.success = False
            result.errors.append(str(exc))
            report_path = self._write_failure_report(ctx, exc, phase=phase)
            if report_path:
                result.errors.append(f"Failure report: {report_path}")
                logger.error("[%s] failure report: %s", self.NAME, report_path)
            ctx.record_stage(result)
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] unexpected error", self.NAME)
            result = self._make_result(dry_run=ctx.dry_run)
            result.success = False
            result.errors.append(f"Unexpected: {exc}")
            report_path = self._write_failure_report(ctx, exc, phase=phase)
            if report_path:
                result.errors.append(f"Failure report: {report_path}")
                logger.error("[%s] failure report: %s", self.NAME, report_path)
            ctx.record_stage(result)
            return result

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.NAME!r})"
