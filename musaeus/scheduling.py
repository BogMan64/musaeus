"""
MUSAEUS — scheduled invocation, aligned with the interactive path (P0-17)

A scheduled run is the same run with nobody watching. It therefore takes
the same typed command, the same preflight, the same lock, the same
authority decision and the same report as an interactive one -- and the
only difference is what it does when the answer is unclear, which is
stop.

Three properties, and the reason each exists:

**Absent input and EOF are never `y`.** `preflight.is_affirmative(None)`
already returns False; this module's job is to make sure `None` is what a
scheduled invocation actually passes, rather than reading stdin and
receiving an empty string from a closed pipe that some later refactor
decides to `.lower().startswith("y")`. A run that mutates because nobody
was there to say no is the worst available failure.

**Preview/review-only by default in P0 and P1.** A schedule expresses
"do this regularly", not "you have my authority in advance". Authority is
a decision made against a specific preflight result, and a cron line
cannot make it.

**A conflict defers; it never forces.** If another run holds the scope,
the scheduled invocation exits with a report naming the owner and tries
again tomorrow. It does not wait indefinitely inside a cron slot, and it
does not steal a lock -- MUSAEUS already has one incident caused by two
processes believing they were alone.

The exit statuses are DR-03's, unchanged from interactive use, so a cron
wrapper's `$?` means the same thing it means at a prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from musaeus.preflight import (
    AuthorityDecision,
    PreflightReport,
    PreflightRequest,
    evaluate_authority,
    run_preflight,
)
from musaeus.reporting import MODE_PREVIEW, ActionCounts, RunReport

# DR-03's stable exit statuses.
EXIT_OK = 0
EXIT_SAFETY_BLOCKED = 2
EXIT_EXECUTION_FAILED = 3

MODE_REVIEW_ONLY = "review-only"


@dataclass(frozen=True)
class ScheduledOutcome:
    """What a scheduled invocation did, and why."""

    exit_code: int
    reason_code: str
    mode: str
    report: RunReport
    preflight: PreflightReport
    authority: AuthorityDecision

    @property
    def mutated(self) -> bool:
        """Always False in P0/P1. Present as an explicit field rather than
        an assumption, so the day it can be True is a visible change."""
        return False


def scheduled_response() -> str | None:
    """
    The authority response a scheduled invocation supplies.

    `None`, always, and deliberately a function rather than a constant so
    that the reason lives at the definition: there is no operator at a
    scheduled run, so there is no answer to the prompt. Reading stdin here
    would be worse than useless -- a closed pipe yields an empty string,
    and an empty string is one careless `.startswith()` away from being
    treated as yes.
    """
    return None


def run_scheduled(
    request: PreflightRequest,
    *,
    run_id: str,
    started_at: str,
    finished_at: str | None = None,
    allow_execution: bool = False,
) -> ScheduledOutcome:
    """
    Run the scheduled path: preflight, authority, report. Mutates nothing.

    `allow_execution` exists so the P0/P1 restriction is a value that can
    be asserted against rather than an absence that has to be inferred. It
    is False, and passing True still cannot grant authority, because
    authority additionally requires an affirmative response and a
    scheduled run has none to give.
    """
    report = run_preflight(request)
    decision = evaluate_authority(report, scheduled_response())

    blocks = tuple(
        {
            "name": check.name,
            "reason_code": check.reason_code,
            "remediation": check.remediation,
            "measured": check.measured,
            "required": check.required,
        }
        for check in report.blocking
    )

    if report.blocked:
        lock_check = next((c for c in report.blocking if c.name == "lock_observation"), None)
        reason = "lock_conflict" if lock_check is not None else "preflight_blocked"
        exit_code = EXIT_SAFETY_BLOCKED
        next_actions = tuple(c.remediation for c in report.blocking if c.remediation)
    else:
        reason = "review_only"
        exit_code = EXIT_OK
        next_actions = (
            "review this preflight and re-run interactively to request execution authority",
        )

    run_report = RunReport(
        run_id=run_id,
        mode=MODE_PREVIEW,
        scope_root=request.scope.root,
        classification=request.scope.classification,
        started_at=started_at,
        finished_at=finished_at,
        status="blocked" if report.blocked else "succeeded",
        exit_code=exit_code,
        reason_code=reason,
        totals=ActionCounts(),
        safety_blocks=blocks,
        lock_observation=report.lock_observation,
        authority="not granted",
        recovery_target=str(request.recovery_root),
        next_actions=next_actions,
    )

    return ScheduledOutcome(
        exit_code=exit_code,
        reason_code=reason,
        mode=MODE_REVIEW_ONLY,
        report=run_report,
        preflight=report,
        authority=decision,
    )


def describe_outcome(outcome: ScheduledOutcome) -> dict[str, Any]:
    """Compact record for a scheduler log line."""
    return {
        "exit_code": outcome.exit_code,
        "reason_code": outcome.reason_code,
        "mode": outcome.mode,
        "authority_granted": outcome.authority.granted,
        "mutated": outcome.mutated,
        "blocking": [b["name"] for b in outcome.report.safety_blocks],
    }
