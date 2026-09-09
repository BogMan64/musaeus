"""
MUSAEUS — the projector (P0-07)

MCR-004 requires that "rebuilding from a canonical event sequence
produces state equivalent to live execution for the same sequence". The
only way to make that true by construction rather than by coincidence is
to have exactly one transition function and use it for both. That
function is `apply_event()`.

Live execution folds events through it one at a time as they are
appended. Rebuild folds the whole persisted sequence through it from an
empty state. `project()` is literally a fold of `apply_event`, so there is
no second implementation that could drift -- which is what happened last
time. `rebuild.py`'s dispatch table listed ten event names, none of which
the pipeline had emitted for a long time; every branch was dead and
nothing said so, because live emission and rebuild interpretation were
two separate pieces of code that agreed only by inheritance from a shared
past.

`apply_event` is pure: it takes a state and an event and returns a new
state. It touches no database, so a projection can be computed and
compared before anything is written anywhere.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from typing import Any

from musaeus.state.events import (
    LEGACY_UNMAPPED,
    RUN_CANCELLATION_REQUESTED,
    RUN_CREATED,
    RUN_PREFLIGHT_COMPLETED,
    RUN_TERMINAL,
    STAGE_BLOCKED,
    STAGE_CANCELLED,
    STAGE_FAILED,
    STAGE_QUEUED,
    STAGE_STARTED,
    STAGE_SUCCEEDED,
    CanonicalEvent,
    read_events,
)
from musaeus.state.schema import StateError

# Run/stage states, from DR-03. Closed set.
PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
BLOCKED = "blocked"

STATES: frozenset[str] = frozenset({PENDING, RUNNING, SUCCEEDED, FAILED, CANCELLED, BLOCKED})


class RebuildBlockedError(StateError):
    """A rebuild was requested for state that cannot be reconstructed.

    Raised, never worked around: the alternative is producing a plausible
    projection over evidence that does not support it."""

    reason_code = "rebuild_blocked"


# ── Projection value types ────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunProjection:
    run_id: str
    status: str = PENDING
    mode: str | None = None
    authority: str | None = None
    config_digest: str | None = None
    scope_id: str | None = None
    exit_code: int | None = None
    reason_code: str | None = None
    checkpoint_id: str | None = None
    rollback_status: str | None = None
    preflight_outcome: str | None = None
    cancellation_requested: bool = False
    blocked_reason: str | None = None
    last_sequence: int = -1


@dataclass(frozen=True)
class StageProjection:
    run_id: str
    stage_id: str
    attempt: int
    status: str = PENDING
    input_digest: str | None = None
    output_digest: str | None = None
    error_code: str | None = None
    safe_to_retry: bool | None = None
    blockers: tuple[str, ...] = ()
    recovery_action: str | None = None
    checkpoint_id: str | None = None
    last_sequence: int = -1


@dataclass(frozen=True)
class UnmappedRecord:
    run_id: str
    sequence: int
    legacy_type: str
    legacy_payload_digest: str
    reason_code: str
    affected_stage: str | None


@dataclass(frozen=True)
class ProjectedState:
    runs: dict[str, RunProjection]
    stages: dict[tuple[str, str, int], StageProjection]
    unmapped: tuple[UnmappedRecord, ...]

    @classmethod
    def empty(cls) -> ProjectedState:
        return cls(runs={}, stages={}, unmapped=())

    def blocked_runs(self) -> tuple[str, ...]:
        """Runs carrying at least one unmappable legacy event."""
        return tuple(sorted({record.run_id for record in self.unmapped}))

    def terminal_stage_statuses(self) -> dict[tuple[str, str, int], str]:
        return {key: stage.status for key, stage in self.stages.items()}


# ── The single transition function ────────────────────────────────────────────


def apply_event(state: ProjectedState, event: CanonicalEvent) -> ProjectedState:
    """
    Fold one event into *state* and return the result.

    Pure: no database, no clock, no I/O. Called once per event by live
    execution and once per event by rebuild -- the same calls in the same
    order over the same sequence, which is what makes parity a property
    rather than a hope.
    """
    runs = dict(state.runs)
    stages = dict(state.stages)
    unmapped = state.unmapped

    run = runs.get(event.run_id, RunProjection(run_id=event.run_id))
    run = replace(run, last_sequence=max(run.last_sequence, event.sequence))
    payload = event.payload

    if event.event_type == RUN_CREATED:
        run = replace(
            run,
            status=RUNNING,
            mode=_as_str(payload.get("mode")),
            authority=_as_str(payload.get("authority")),
            config_digest=_as_str(payload.get("config_digest")),
            scope_id=event.scope_id,
        )
    elif event.event_type == RUN_PREFLIGHT_COMPLETED:
        run = replace(run, preflight_outcome=_as_str(payload.get("outcome")))
    elif event.event_type == RUN_CANCELLATION_REQUESTED:
        run = replace(run, cancellation_requested=True)
    elif event.event_type == RUN_TERMINAL:
        status = _as_str(payload.get("status"))
        run = replace(
            run,
            status=status if status in STATES else FAILED,
            exit_code=_as_int(payload.get("exit_code")),
            reason_code=_as_str(payload.get("reason_code")),
            checkpoint_id=_as_str(payload.get("checkpoint_id")),
            rollback_status=_as_str(payload.get("rollback_status")),
        )
    elif event.event_type == LEGACY_UNMAPPED:
        record = UnmappedRecord(
            run_id=event.run_id,
            sequence=event.sequence,
            legacy_type=str(payload["legacy_type"]),
            legacy_payload_digest=str(payload["legacy_payload_digest"]),
            reason_code=str(payload["reason_code"]),
            affected_stage=_as_str(payload.get("affected_stage")),
        )
        unmapped = (*unmapped, record)
        # A single unmappable event poisons the whole run's reconstruction.
        # Deliberately not "poisons that stage": the event's own stage
        # attribution is part of what could not be reconstructed, so
        # scoping the block to a stage would be trusting the very field
        # whose meaning is in doubt.
        run = replace(
            run,
            status=BLOCKED,
            blocked_reason=(
                f"legacy evidence could not be mapped to the canonical vocabulary "
                f"({record.legacy_type}: {record.reason_code})"
            ),
        )
    elif event.event_type in _STAGE_EVENTS:
        stage_id = str(payload["stage_id"])
        attempt = int(payload["attempt"])
        key = (event.run_id, stage_id, attempt)
        stage = stages.get(
            key, StageProjection(run_id=event.run_id, stage_id=stage_id, attempt=attempt)
        )
        stage = replace(stage, last_sequence=max(stage.last_sequence, event.sequence))
        stage = _apply_stage_event(stage, event, payload)
        stages[key] = stage

    runs[event.run_id] = run
    return ProjectedState(runs=runs, stages=stages, unmapped=unmapped)


_STAGE_EVENTS: frozenset[str] = frozenset(
    {STAGE_QUEUED, STAGE_STARTED, STAGE_SUCCEEDED, STAGE_FAILED, STAGE_CANCELLED, STAGE_BLOCKED}
)


def _apply_stage_event(
    stage: StageProjection, event: CanonicalEvent, payload: dict[str, Any]
) -> StageProjection:
    if event.event_type == STAGE_QUEUED:
        return replace(stage, status=PENDING, input_digest=_as_str(payload.get("input_digest")))
    if event.event_type == STAGE_STARTED:
        return replace(stage, status=RUNNING, input_digest=_as_str(payload.get("input_digest")))
    if event.event_type == STAGE_SUCCEEDED:
        return replace(
            stage,
            status=SUCCEEDED,
            input_digest=_as_str(payload.get("input_digest")),
            output_digest=_as_str(payload.get("output_digest")),
        )
    if event.event_type == STAGE_FAILED:
        return replace(
            stage,
            status=FAILED,
            error_code=_as_str(payload.get("error_code")),
            safe_to_retry=_as_bool(payload.get("safe_to_retry")),
            checkpoint_id=_as_str(payload.get("checkpoint_id")),
        )
    if event.event_type == STAGE_CANCELLED:
        return replace(stage, status=CANCELLED, checkpoint_id=_as_str(payload.get("checkpoint_id")))
    # STAGE_BLOCKED
    blockers = payload.get("blockers", [])
    return replace(
        stage,
        status=BLOCKED,
        blockers=tuple(
            json.dumps(b, sort_keys=True) if isinstance(b, dict) else str(b) for b in blockers
        ),
        recovery_action=_as_str(payload.get("recovery_action")),
    )


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _as_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _as_bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


# ── Fold ──────────────────────────────────────────────────────────────────────


def project(events: object) -> ProjectedState:
    """Fold a whole sequence. Rebuild's entry point; live execution folds
    the same function one event at a time."""
    state = ProjectedState.empty()
    for event in events:  # type: ignore[attr-defined]
        state = apply_event(state, event)
    return state


def assert_rebuildable(state: ProjectedState) -> None:
    """Raise RebuildBlockedError when any run carries unmapped evidence."""
    blocked = state.blocked_runs()
    if blocked:
        details = {record.legacy_type for record in state.unmapped}
        raise RebuildBlockedError(
            f"{len(blocked)} run(s) carry legacy evidence that cannot be mapped to the "
            f"canonical vocabulary and cannot be rebuilt: {', '.join(blocked)}",
            blocked_runs=list(blocked),
            legacy_types=sorted(details),
            remediation=(
                "recover from the pre-reset database snapshot or from disk + embedded "
                "tags; the legacy event log is lossy and cannot reconstruct this state"
            ),
        )


# ── Persistence of the projection ─────────────────────────────────────────────

PROJECTION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS projected_runs (
        run_id                 TEXT PRIMARY KEY,
        status                 TEXT NOT NULL,
        mode                   TEXT,
        authority              TEXT,
        config_digest          TEXT,
        scope_id               TEXT,
        exit_code              INTEGER,
        reason_code            TEXT,
        checkpoint_id          TEXT,
        rollback_status        TEXT,
        preflight_outcome      TEXT,
        cancellation_requested INTEGER NOT NULL DEFAULT 0,
        blocked_reason         TEXT,
        last_sequence          INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projected_stages (
        run_id          TEXT NOT NULL,
        stage_id        TEXT NOT NULL,
        attempt         INTEGER NOT NULL,
        status          TEXT NOT NULL,
        input_digest    TEXT,
        output_digest   TEXT,
        error_code      TEXT,
        safe_to_retry   INTEGER,
        blockers        TEXT,
        recovery_action TEXT,
        checkpoint_id   TEXT,
        last_sequence   INTEGER NOT NULL,
        PRIMARY KEY (run_id, stage_id, attempt)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projected_unmapped (
        run_id                TEXT NOT NULL,
        sequence              INTEGER NOT NULL,
        legacy_type           TEXT NOT NULL,
        legacy_payload_digest TEXT NOT NULL,
        reason_code           TEXT NOT NULL,
        affected_stage        TEXT,
        PRIMARY KEY (run_id, sequence)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_projected_stages_run ON projected_stages(run_id)",
)


def write_projection(conn: sqlite3.Connection, state: ProjectedState) -> None:
    """Replace the persisted projection with *state*.

    Derived state, wholly rebuildable from the canonical events, so
    replacing it is safe in a way that `DELETE FROM archive` never was:
    the archive holds metadata that exists nowhere else, while these three
    tables hold nothing that the event sequence does not already contain.
    """
    conn.execute("DELETE FROM projected_runs")
    conn.execute("DELETE FROM projected_stages")
    conn.execute("DELETE FROM projected_unmapped")

    for run in state.runs.values():
        conn.execute(
            """
            INSERT INTO projected_runs
                (run_id, status, mode, authority, config_digest, scope_id, exit_code,
                 reason_code, checkpoint_id, rollback_status, preflight_outcome,
                 cancellation_requested, blocked_reason, last_sequence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.status,
                run.mode,
                run.authority,
                run.config_digest,
                run.scope_id,
                run.exit_code,
                run.reason_code,
                run.checkpoint_id,
                run.rollback_status,
                run.preflight_outcome,
                int(run.cancellation_requested),
                run.blocked_reason,
                run.last_sequence,
            ),
        )
    for stage in state.stages.values():
        conn.execute(
            """
            INSERT INTO projected_stages
                (run_id, stage_id, attempt, status, input_digest, output_digest,
                 error_code, safe_to_retry, blockers, recovery_action, checkpoint_id,
                 last_sequence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stage.run_id,
                stage.stage_id,
                stage.attempt,
                stage.status,
                stage.input_digest,
                stage.output_digest,
                stage.error_code,
                None if stage.safe_to_retry is None else int(stage.safe_to_retry),
                json.dumps(list(stage.blockers)),
                stage.recovery_action,
                stage.checkpoint_id,
                stage.last_sequence,
            ),
        )
    for record in state.unmapped:
        conn.execute(
            """
            INSERT INTO projected_unmapped
                (run_id, sequence, legacy_type, legacy_payload_digest, reason_code,
                 affected_stage)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record.run_id,
                record.sequence,
                record.legacy_type,
                record.legacy_payload_digest,
                record.reason_code,
                record.affected_stage,
            ),
        )


def rebuild_projection(conn: sqlite3.Connection, *, run_id: str | None = None) -> ProjectedState:
    """Recompute the projection from the persisted canonical events.

    Reads and folds; writes nothing. Whether the result is adopted is the
    caller's decision (P0-06's candidate-swap path), which keeps "compute a
    candidate" and "replace live state" as two separately auditable acts
    rather than one function that does both and reports success."""
    return project(read_events(conn, run_id))


# ── Parity ────────────────────────────────────────────────────────────────────


def projection_parity(live: ProjectedState, rebuilt: ProjectedState) -> list[str]:
    """
    Return a list of differences between two projections; empty means
    equivalent.

    Compares field by field, not row counts. MCR-004 is explicit that
    parity means "event names, payload meaning, and terminal stage
    status", and two projections with identical row counts and different
    terminal statuses are exactly the failure this is meant to catch.
    """
    diffs: list[str] = []

    live_runs, rebuilt_runs = set(live.runs), set(rebuilt.runs)
    if live_runs != rebuilt_runs:
        diffs.append(
            f"run ids differ: only-live={sorted(live_runs - rebuilt_runs)}, "
            f"only-rebuilt={sorted(rebuilt_runs - live_runs)}"
        )
    for run_id in sorted(live_runs & rebuilt_runs):
        if live.runs[run_id] != rebuilt.runs[run_id]:
            diffs.append(f"run {run_id} differs: {live.runs[run_id]} != {rebuilt.runs[run_id]}")

    live_stages, rebuilt_stages = set(live.stages), set(rebuilt.stages)
    if live_stages != rebuilt_stages:
        diffs.append(
            f"stage keys differ: only-live={sorted(live_stages - rebuilt_stages)}, "
            f"only-rebuilt={sorted(rebuilt_stages - live_stages)}"
        )
    for key in sorted(live_stages & rebuilt_stages):
        if live.stages[key] != rebuilt.stages[key]:
            diffs.append(f"stage {key} differs: {live.stages[key]} != {rebuilt.stages[key]}")

    if live.unmapped != rebuilt.unmapped:
        diffs.append(
            f"unmapped evidence differs: live={len(live.unmapped)}, rebuilt={len(rebuilt.unmapped)}"
        )
    return diffs
