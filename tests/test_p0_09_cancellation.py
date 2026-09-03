"""
P0-09 — durable cancellation, bounded recording, truthful terminal state.

Uses the P0-01 harness's FakeClock rather than real time, so the 30-second
bound is asserted against a controlled clock instead of being a test that
passes because the machine happened to be fast.

Post-mutation *restoration* is P0-13's proof. What is proven here is the
decision layer around it: that a cancelled run which mutated something
cannot write a terminal outcome at all until recovery has reported a
result, and that a failed rollback lands as `failed`, never `cancelled`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from musaeus.state.cancellation import (
    CANCELLATION_BOUND_SECONDS,
    EXIT_CANCELLED_RECOVERED,
    EXIT_EXECUTION_FAILED,
    ROLLBACK_COMPLETED,
    ROLLBACK_FAILED,
    ROLLBACK_NOT_REQUIRED,
    CancellationBoundExceeded,
    CancellationGate,
    MutationAfterCancellationError,
    RecoveryNotHandledError,
    assert_recorded_within_bound,
    build_cancellation_terminal,
    request_cancellation,
    terminal_event,
)
from musaeus.state.events import (
    RUN_CANCELLATION_REQUESTED,
    append_event,
    read_events,
)
from musaeus.state.migrator import migrate
from musaeus.state.projector import CANCELLED, FAILED, apply_event, project
from musaeus.state.run_state import REASON_RUN_CANCELLED, evaluate_gating, linear_graph
from tests.disposable_vault import FakeClock
from tests.test_p0_08_run_lifecycle import PIPELINE, _run_created, _succeeded


@pytest.fixture
def migrated_db(disposable_vault) -> Path:
    conn = disposable_vault.open_db()
    conn.close()
    migrate(disposable_vault.cfg.db_path, recovery_root=disposable_vault.recovery_root)
    return disposable_vault.cfg.db_path


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


# ── Durability ────────────────────────────────────────────────────────────────


class TestCancellationIsDurable:
    def test_request_survives_the_process_that_made_it(self, migrated_db):
        """Written to the database, not held in memory. The connection that
        made the request is closed before the assertion, standing in for
        the process that made it going away."""
        conn = _connect(migrated_db)
        try:
            append_event(conn, _run_created())
            request_cancellation(
                conn, "run-A", 1, requested_by="grey", reason_code="operator_request"
            )
        finally:
            conn.close()

        conn = _connect(migrated_db)
        try:
            events = read_events(conn, "run-A")
        finally:
            conn.close()
        assert events[1].event_type == RUN_CANCELLATION_REQUESTED
        assert events[1].payload["requested_by"] == "grey"
        assert project(events).runs["run-A"].cancellation_requested is True

    def test_request_is_idempotent_on_replay(self, migrated_db):
        conn = _connect(migrated_db)
        try:
            append_event(conn, _run_created())
            event = request_cancellation(conn, "run-A", 1, requested_by="grey")
            assert append_event(conn, event) is False
            count = conn.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0]
        finally:
            conn.close()
        assert count == 2


# ── The 30-second bound ───────────────────────────────────────────────────────


class TestRecordingBound:
    def test_prompt_recording_is_within_bound(self):
        clock = FakeClock()
        requested = clock.utcnow_iso()
        clock.advance(seconds=2)
        latency = assert_recorded_within_bound(requested, clock.utcnow_iso())
        assert latency == pytest.approx(2.0)

    def test_recording_at_exactly_the_bound_is_accepted(self):
        clock = FakeClock()
        requested = clock.utcnow_iso()
        clock.advance(seconds=CANCELLATION_BOUND_SECONDS)
        assert assert_recorded_within_bound(requested, clock.utcnow_iso()) == pytest.approx(30.0)

    def test_late_recording_is_refused(self):
        clock = FakeClock()
        requested = clock.utcnow_iso()
        clock.advance(seconds=CANCELLATION_BOUND_SECONDS + 1)
        with pytest.raises(CancellationBoundExceeded) as exc:
            assert_recorded_within_bound(requested, clock.utcnow_iso())
        assert exc.value.reason_code == "cancellation_not_recorded_in_time"

    def test_a_backwards_clock_is_refused_rather_than_read_as_fast(self):
        clock = FakeClock()
        requested = clock.utcnow_iso()
        clock.advance(seconds=-5)
        with pytest.raises(CancellationBoundExceeded) as exc:
            assert_recorded_within_bound(requested, clock.utcnow_iso())
        assert "not monotonic" in str(exc.value)


# ── Observation and mutation refusal ──────────────────────────────────────────


class TestObservation:
    def test_gate_is_inert_until_cancellation_is_requested(self):
        state = project((_run_created(), _succeeded("run-A", 1, "IngestStage")))
        gate = CancellationGate.from_state(state, "run-A")
        assert gate.requested is False
        assert gate.observe() is False
        assert gate.observed is False
        gate.record_mutation()  # must not raise
        assert gate.mutations_applied == 1

    def test_observation_stops_further_mutation(self, migrated_db):
        clock = FakeClock()
        conn = _connect(migrated_db)
        try:
            append_event(conn, _run_created())
            request_cancellation(conn, "run-A", 1, requested_by="grey", now=clock.utcnow_iso())
            state = project(read_events(conn, "run-A"))
        finally:
            conn.close()

        gate = CancellationGate.from_state(state, "run-A")
        assert gate.observe(clock.utcnow_iso()) is True

        with pytest.raises(MutationAfterCancellationError) as exc:
            gate.record_mutation()
        assert exc.value.reason_code == "mutation_after_cancellation"

    def test_first_observation_timestamp_is_kept(self):
        clock = FakeClock()
        gate = CancellationGate(run_id="run-A", requested=True)
        gate.observe(clock.utcnow_iso())
        first = gate.observed_at
        clock.advance(minutes=5)
        gate.observe(clock.utcnow_iso())
        assert gate.observed_at == first, "the moment mutation had to stop does not move"

    def test_no_downstream_stage_starts_after_cancellation(self):
        """MCR-005: no new stage begins once cancellation is requested.
        Enforced in the gate rather than per stage, so a stage cannot be
        written that forgets to ask."""
        events = (
            _run_created(),
            _succeeded("run-A", 1, "IngestStage"),
        )
        state = project(events)
        graph = linear_graph(PIPELINE)
        assert evaluate_gating(graph, state, "run-A")["ForgeStage"].eligible is True

        from musaeus.state.events import new_event

        cancelled_state = apply_event(
            state,
            new_event(
                "run-A",
                2,
                RUN_CANCELLATION_REQUESTED,
                {
                    "requested_by": "grey",
                    "requested_at": "2026-08-23T22:00:00Z",
                    "reason_code": "operator_request",
                },
            ),
        )
        decisions = evaluate_gating(graph, cancelled_state, "run-A")
        assert all(not d.eligible for d in decisions.values())
        assert decisions["ForgeStage"].blockers[0].reason_code == REASON_RUN_CANCELLED
        assert "do not start further work" in decisions["ForgeStage"].recovery_action


# ── Terminal outcomes ─────────────────────────────────────────────────────────


class TestTerminalOutcome:
    def test_cancelled_before_any_mutation_is_a_clean_cancellation(self):
        gate = CancellationGate(run_id="run-A", requested=True)
        gate.observe("2026-08-23T22:00:00Z")
        outcome = build_cancellation_terminal(gate)
        assert outcome.status == CANCELLED
        assert outcome.exit_code == EXIT_CANCELLED_RECOVERED
        assert outcome.rollback_status == ROLLBACK_NOT_REQUIRED

    def test_a_mutated_run_cannot_write_a_terminal_without_a_rollback_result(self):
        """The core refusal. A cancelled run that changed things and cannot
        say what happened to those changes has no truthful terminal state
        available, so it is not offered one."""
        gate = CancellationGate(run_id="run-A", requested=False)
        gate.record_mutation()
        gate.record_mutation()
        gate.requested = True
        gate.observe("2026-08-23T22:00:00Z")

        with pytest.raises(RecoveryNotHandledError) as exc:
            build_cancellation_terminal(gate)
        assert exc.value.reason_code == "recovery_not_handled"
        assert exc.value.details["mutations_applied"] == 2

    def test_successful_rollback_yields_cancelled(self):
        gate = CancellationGate(run_id="run-A", requested=False)
        gate.record_mutation()
        gate.requested = True
        gate.observe("2026-08-23T22:00:00Z")
        outcome = build_cancellation_terminal(gate, rollback_status=ROLLBACK_COMPLETED)
        assert outcome.status == CANCELLED
        assert outcome.exit_code == EXIT_CANCELLED_RECOVERED
        assert outcome.reason_code == "cancelled_after_rollback"

    def test_failed_rollback_yields_failed_not_cancelled(self):
        """MCR-003. Reporting `cancelled` here would describe an orderly
        stop over a library that is still half-changed."""
        gate = CancellationGate(run_id="run-A", requested=False)
        gate.record_mutation()
        gate.requested = True
        gate.observe("2026-08-23T22:00:00Z")
        outcome = build_cancellation_terminal(gate, rollback_status=ROLLBACK_FAILED)
        assert outcome.status == FAILED
        assert outcome.exit_code == EXIT_EXECUTION_FAILED
        assert outcome.reason_code == "rollback_failed"

    def test_not_required_is_refused_when_mutations_happened(self):
        gate = CancellationGate(run_id="run-A", requested=True)
        gate.mutations_applied = 3
        with pytest.raises(RecoveryNotHandledError):
            build_cancellation_terminal(gate, rollback_status=ROLLBACK_NOT_REQUIRED)

    def test_unknown_rollback_status_is_refused(self):
        gate = CancellationGate(run_id="run-A", requested=True)
        gate.mutations_applied = 1
        with pytest.raises(RecoveryNotHandledError) as exc:
            build_cancellation_terminal(gate, rollback_status="probably_fine")
        assert "expected one of" in str(exc.value)

    def test_cancelled_work_is_never_projected_as_complete(self, migrated_db):
        """End to end through the real append/project path: request,
        observe, terminal, and confirm the projection says cancelled."""
        gate = CancellationGate(run_id="run-A", requested=True)
        gate.observe("2026-08-23T22:00:00Z")
        outcome = build_cancellation_terminal(gate)

        conn = _connect(migrated_db)
        try:
            append_event(conn, _run_created())
            append_event(conn, _succeeded("run-A", 1, "IngestStage"))
            request_cancellation(conn, "run-A", 2, requested_by="grey")
            append_event(
                conn,
                terminal_event("run-A", 3, outcome, stage_counts={"succeeded": 1}),
            )
            state = project(read_events(conn, "run-A"))
        finally:
            conn.close()

        run = state.runs["run-A"]
        assert run.status == CANCELLED
        assert run.status != "succeeded"
        assert run.exit_code == EXIT_CANCELLED_RECOVERED
        assert run.rollback_status == ROLLBACK_NOT_REQUIRED
        assert run.cancellation_requested is True

    def test_terminal_event_payload_validates(self):
        gate = CancellationGate(run_id="run-A", requested=True)
        gate.observe()
        event = terminal_event(
            "run-A", 5, build_cancellation_terminal(gate), stage_counts={"succeeded": 2}
        )
        assert event.payload["status"] == CANCELLED
        assert event.payload["stage_counts"] == {"succeeded": 2}
