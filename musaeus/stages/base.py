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

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
    def run(self, ctx: "RunContext") -> "StageResult":
        """Execute the stage. May write files, update DB, move files."""
        ...

    @abstractmethod
    def dry_run(self, ctx: "RunContext") -> "StageResult":
        """
        Report what run() would do — NO mutations.
        Must be implemented. Never a no-op.
        """
        ...

    @abstractmethod
    def validate(self, ctx: "RunContext") -> None:
        """
        Pre-flight checks. Raise StageError if prerequisites are not met.
        Called before run() or dry_run().
        """
        ...

    # ── Helper ────────────────────────────────────────────────────────────────

    def _make_result(self, dry_run: bool = False) -> "StageResult":
        """Create a fresh StageResult for this stage."""
        from ..context import StageResult

        return StageResult(stage_name=self.NAME, success=True, dry_run=dry_run)

    def execute(self, ctx: "RunContext") -> "StageResult":
        """
        Public entry point called by the pipeline runner.
        Runs validate(), then run() or dry_run() depending on ctx.dry_run.
        Wraps exceptions into a failed StageResult so the pipeline continues.
        """
        try:
            self.validate(ctx)
        except StageError as exc:
            logger.error("[%s] validation failed: %s", self.NAME, exc)
            result = self._make_result(dry_run=ctx.dry_run)
            result.success = False
            result.errors.append(f"ValidationError: {exc}")
            ctx.record_stage(result)
            return result

        try:
            if ctx.dry_run:
                return self.dry_run(ctx)
            return self.run(ctx)
        except StageError as exc:
            logger.error("[%s] stage error: %s", self.NAME, exc)
            result = self._make_result(dry_run=ctx.dry_run)
            result.success = False
            result.errors.append(str(exc))
            ctx.record_stage(result)
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] unexpected error", self.NAME)
            result = self._make_result(dry_run=ctx.dry_run)
            result.success = False
            result.errors.append(f"Unexpected: {exc}")
            ctx.record_stage(result)
            return result

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.NAME!r})"
