"""
MUSAEUS — durable cancellation and recovery-aware terminal outcomes (P0-09)

Cancellation here is a *durable request*, not a signal handler and not an
in-memory flag. It is appended to the canonical event log, so a process
killed immediately afterwards leaves the request behind rather than
losing it -- which is the whole difference between "the user asked to
stop" and "the user asked to stop and we have no record of it".

The rule this module exists to make unavoidable:

    A run that mutated something and was then cancelled may not write a
    terminal outcome until recovery has actually been handled.

`build_cancellation_terminal()` therefore REFUSES to produce a payload
when mutations were applied and no rollback result is supplied. Not
"defaults to unknown", not "warns" -- refuses. The alternative is a
`run.terminal` that says `cancelled` over a half-mutated library, which
is the same lie in a different costume as a stage that reports success
having done nothing. And per MCR-003, if that rollback failed, the run is
`failed`, not `cancelled`: recovery material is preserved and further
mutation is blocked.

The 30-second bound in MCR-005 is on *recording*, not on stopping.
Recording is cheap and must be prompt; reaching a safe checkpoint may
legitimately take longer, and conflating the two would push stages to
abandon work mid-write to hit a deadline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from musaeus.state.events import (
    RUN_CANCELLATION_REQUESTED,
    RUN_TERMINAL,
    CanonicalEvent,
    append_event,
    new_event,
)
from musaeus.state.projector import CANCELLED, FAILED, ProjectedState
from musaeus.state.schema import StateError, utc_now_iso

# MCR-005: "A cancellation request is recorded within 30 seconds".
CANCELLATION_BOUND_SECONDS: int = 30

# Exit statuses, from DR-03's stable table.
EXIT_SUCCESS = 0
EXIT_SAFETY_BLOCKED = 2
EXIT_EXECUTION_FAILED = 3
EXIT_CANCELLED_RECOVERED = 4

# Rollback outcome vocabulary. Closed.
ROLLBACK_NOT_REQUIRED = "not_required"
ROLLBACK_COMPLETED = "completed"
ROLLBACK_FAILED = "failed"
ROLLBACK_OUTCOMES: frozenset[str] = frozenset(
    {ROLLBACK_NOT_REQUIRED, ROLLBACK_COMPLETED, ROLLBACK_FAILED}
)


class CancellationBoundExceeded(StateError):
    """A cancellation request was not recorded within the bound."""

    reason_code = "cancellation_not_recorded_in_time"


class MutationAfterCancellationError(StateError):
    """A mutation was attempted after cancellation had been observed."""

    reason_code = "mutation_after_cancellation"


class RecoveryNotHandledError(StateError):
    """A terminal outcome was requested for a cancelled run that applied
    mutations, without a rollback result. Refused: the terminal record
    would be a claim nobody has verified."""

    reason_code = "recovery_not_handled"


# ── Requesting ────────────────────────────────────────────────────────────────


def request_cancellation(
    conn,
    run_id: str,
    sequence: int,
    *,
    requested_by: str,
    reason_code: str = "operator_request",
    now: str | None = None,
) -> CanonicalEvent:
    """
    Durably record a cancellation request and return the event.

    Written through the canonical append path, so it inherits the sequence
    and idempotence rules: requesting twice with the same event_id is a
    no-op, and the request cannot silently overwrite another event.
    """
    timestamp = now if now is not None else utc_now_iso()
    event = new_event(
        run_id,
        sequence,
        RUN_CANCELLATION_REQUESTED,
        {
            "requested_by": requested_by,
            "requested_at": timestamp,
            "reason_code": reason_code,
        },
        occurred_at=timestamp,
    )
    append_event(conn, event)
    return event


def _parse(timestamp: str) -> datetime:
    normalised = timestamp.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalised)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def assert_recorded_within_bound(
    requested_at: str, recorded_at: str, *, bound_seconds: int = CANCELLATION_BOUND_SECONDS
) -> float:
    """
    Return the recording latency in seconds; raise if it exceeds the bound.

    The bound applies to *recording* the request, not to reaching a safe
    checkpoint. Recording is a single append and must be prompt; a stage
    finishing a write it has already started may take longer, and holding
    it to the same deadline would encourage abandoning work mid-write --
    trading a slow stop for a corrupt one.
    """
    latency = (_parse(recorded_at) - _parse(requested_at)).total_seconds()
    if latency < 0:
        raise CancellationBoundExceeded(
            f"cancellation recorded at {recorded_at}, before it was requested at "
            f"{requested_at}; the clock is not monotonic here",
            requested_at=requested_at,
            recorded_at=recorded_at,
        )
    if latency > bound_seconds:
        raise CancellationBoundExceeded(
            f"cancellation took {latency:.1f}s to record, exceeding the {bound_seconds}s bound",
            latency_seconds=latency,
            bound_seconds=bound_seconds,
        )
    return latency


# ── Observing ─────────────────────────────────────────────────────────────────


@dataclass
class CancellationGate:
    """
    What a stage consults at a bounded safe checkpoint.

    Mutable by design: `observe()` is the moment the run learns it should
    stop, and that moment is exactly what must be recorded. Once observed,
    `guard_mutation()` raises for every subsequent mutation attempt --
    a positive refusal rather than a boolean a caller can forget to check.
    """

    run_id: str
    requested: bool = False
    requested_at: str | None = None
    observed_at: str | None = None
    mutations_applied: int = 0

    @classmethod
    def from_state(cls, state: ProjectedState, run_id: str) -> CancellationGate:
        run = state.runs.get(run_id)
        requested = run is not None and run.cancellation_requested
        return cls(run_id=run_id, requested=requested)

    @property
    def observed(self) -> bool:
        return self.observed_at is not None

    def observe(self, now: str | None = None) -> bool:
        """Called at a safe checkpoint. Returns True when the run should
        stop. Idempotent: the first observation's timestamp is kept, since
        that is when new mutation actually had to stop."""
        if not self.requested:
            return False
        if self.observed_at is None:
            self.observed_at = now if now is not None else utc_now_iso()
        return True

    def guard_mutation(self) -> None:
        """Raise if a mutation is attempted after cancellation was observed."""
        if self.observed:
            raise MutationAfterCancellationError(
                f"run {self.run_id} observed cancellation at {self.observed_at}; "
                f"no new mutation may begin",
                run_id=self.run_id,
                observed_at=self.observed_at,
            )

    def record_mutation(self) -> None:
        """Count an applied mutation, refusing if cancellation was observed.

        Counting matters: it is what decides whether a terminal outcome may
        be written without a rollback result."""
        self.guard_mutation()
        self.mutations_applied += 1


# ── Terminal outcomes ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TerminalOutcome:
    status: str
    exit_code: int
    reason_code: str
    rollback_status: str

    def as_payload(
        self, *, stage_counts: dict[str, int], checkpoint_id: str | None
    ) -> dict[str, object]:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "reason_code": self.reason_code,
            "stage_counts": dict(stage_counts),
            "checkpoint_id": checkpoint_id,
            "rollback_status": self.rollback_status,
        }


def build_cancellation_terminal(
    gate: CancellationGate, *, rollback_status: str | None = None
) -> TerminalOutcome:
    """
    Decide the truthful terminal outcome for a cancelled run.

      * nothing mutated                  -> cancelled, exit 4, not_required
      * mutated and rollback completed   -> cancelled, exit 4, completed
      * mutated and rollback failed      -> FAILED,    exit 3, failed
      * mutated and no rollback result   -> refuse

    The last case is the point of the function. A cancelled run that
    changed things and cannot say what happened to those changes has no
    truthful terminal state available to it, so it is not offered one.
    """
    if rollback_status is not None and rollback_status not in ROLLBACK_OUTCOMES:
        raise RecoveryNotHandledError(
            f"unknown rollback status {rollback_status!r}; expected one of "
            f"{sorted(ROLLBACK_OUTCOMES)}",
            rollback_status=rollback_status,
        )

    if gate.mutations_applied == 0:
        return TerminalOutcome(
            status=CANCELLED,
            exit_code=EXIT_CANCELLED_RECOVERED,
            reason_code="cancelled_before_mutation",
            rollback_status=ROLLBACK_NOT_REQUIRED,
        )

    if rollback_status is None:
        raise RecoveryNotHandledError(
            f"run {gate.run_id} applied {gate.mutations_applied} mutation(s) before "
            f"cancellation; a terminal outcome cannot be written until the recovery "
            f"path has run and reported a result",
            run_id=gate.run_id,
            mutations_applied=gate.mutations_applied,
            remediation="run the rollback and pass its result as rollback_status",
        )

    if rollback_status == ROLLBACK_FAILED:
        # MCR-003: a failed rollback leaves the run failed, preserves the
        # checkpoint and quarantine, and blocks further mutation. Reporting
        # `cancelled` here would describe an orderly stop over a library
        # that is still half-changed.
        return TerminalOutcome(
            status=FAILED,
            exit_code=EXIT_EXECUTION_FAILED,
            reason_code="rollback_failed",
            rollback_status=ROLLBACK_FAILED,
        )

    if rollback_status == ROLLBACK_NOT_REQUIRED:
        raise RecoveryNotHandledError(
            f"run {gate.run_id} applied {gate.mutations_applied} mutation(s); rollback "
            f"cannot be 'not_required'",
            run_id=gate.run_id,
            mutations_applied=gate.mutations_applied,
        )

    return TerminalOutcome(
        status=CANCELLED,
        exit_code=EXIT_CANCELLED_RECOVERED,
        reason_code="cancelled_after_rollback",
        rollback_status=ROLLBACK_COMPLETED,
    )


def terminal_event(
    run_id: str,
    sequence: int,
    outcome: TerminalOutcome,
    *,
    stage_counts: dict[str, int] | None = None,
    checkpoint_id: str | None = None,
    now: str | None = None,
) -> CanonicalEvent:
    """Build the validated `run.terminal` event for *outcome*."""
    return new_event(
        run_id,
        sequence,
        RUN_TERMINAL,
        outcome.as_payload(stage_counts=stage_counts or {}, checkpoint_id=checkpoint_id),
        occurred_at=now,
    )
