"""
MUSAEUS — run/stage lifecycle, prerequisite gating, resume eligibility (P0-08)

Three rules, and every one of them exists because the opposite has
already happened in this project:

1. **A failed stage is not skipped by resume.** `tests/
   test_p0_01_characterization.py` still carries the regression proving
   the current resume path records a failed stage as complete. Resume
   here skips only a stage whose recorded state says `succeeded` AND
   whose digests still validate; everything else -- failed, cancelled,
   blocked, absent, legacy-unmapped, digest-mismatched -- is re-run or
   blocked, never assumed.

2. **A blocked stage names its blocker and a recovery action.** MCR-005
   requires a deliberately failed prerequisite to "identify the blocking
   stage and recovery action". Blockers are typed objects, not prose, so
   a caller can act on them rather than print them.

3. **A new attempt is explicit.** `failed | cancelled | blocked ->
   pending` is legal only as a newly recorded attempt, never as an
   in-place reset of the old one. Reusing the attempt number would erase
   the evidence that something failed, which is the same class of erasure
   as a rollback that eats its own ledger row.

This module is a pure decision service: it reads a `ProjectedState`,
consults a `StageGraph`, and returns decisions. It performs no I/O, holds
no connection, and mutates nothing -- so a resume plan can be computed
and inspected before anything is allowed to act on it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from musaeus.state.projector import (
    BLOCKED,
    CANCELLED,
    FAILED,
    PENDING,
    RUNNING,
    STATES,
    SUCCEEDED,
    ProjectedState,
    StageProjection,
)
from musaeus.state.schema import StateError


class TransitionError(StateError):
    """A state transition outside DR-03's permitted set."""

    reason_code = "invalid_transition"


class StageGraphError(StateError):
    """The declared stage graph is not usable: unknown dependency or cycle."""

    reason_code = "stage_graph_invalid"


# ── Transitions (DR-03) ───────────────────────────────────────────────────────

# pending   -> running | blocked
# running   -> succeeded | failed | cancelled
# terminal  -> pending, ONLY as a new recorded attempt
# succeeded -> blocked, ONLY when recorded inputs/outputs are invalidated
_BASE_TRANSITIONS: dict[str, frozenset[str]] = {
    PENDING: frozenset({RUNNING, BLOCKED}),
    RUNNING: frozenset({SUCCEEDED, FAILED, CANCELLED}),
    SUCCEEDED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
    BLOCKED: frozenset(),
}

_NEW_ATTEMPT_SOURCES: frozenset[str] = frozenset({FAILED, CANCELLED, BLOCKED})


def check_transition(
    current: str, target: str, *, new_attempt: bool = False, invalidated: bool = False
) -> None:
    """
    Raise TransitionError unless `current -> target` is permitted.

    `new_attempt` and `invalidated` are the two named escape hatches DR-03
    allows, and they are parameters rather than implicit behaviour so that
    a caller has to say which one it is invoking. An implicit reset is how
    a failed stage becomes a pending stage becomes a successful stage with
    nobody having decided anything.
    """
    for label, value in (("current", current), ("target", target)):
        if value not in STATES:
            raise TransitionError(
                f"{label} state {value!r} is not one of {sorted(STATES)}", offending=value
            )

    if target in _BASE_TRANSITIONS[current]:
        return
    if new_attempt and current in _NEW_ATTEMPT_SOURCES and target == PENDING:
        return
    if invalidated and current == SUCCEEDED and target == BLOCKED:
        return

    hint = ""
    if current in _NEW_ATTEMPT_SOURCES and target == PENDING:
        hint = "; a terminal stage returns to pending only as a new recorded attempt"
    elif current == SUCCEEDED and target == BLOCKED:
        hint = "; a succeeded stage becomes blocked only when its inputs/outputs are invalidated"
    raise TransitionError(
        f"transition {current} -> {target} is not permitted{hint}",
        current=current,
        target=target,
    )


# ── Stage graph ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StageDefinition:
    stage_id: str
    depends_on: tuple[str, ...] = ()
    mutates: bool = False


@dataclass(frozen=True)
class StageGraph:
    stages: tuple[StageDefinition, ...]

    def __post_init__(self) -> None:
        known = {s.stage_id for s in self.stages}
        if len(known) != len(self.stages):
            raise StageGraphError("duplicate stage_id in graph")
        for stage in self.stages:
            unknown = sorted(set(stage.depends_on) - known)
            if unknown:
                raise StageGraphError(
                    f"{stage.stage_id} depends on unknown stage(s): {', '.join(unknown)}",
                    stage_id=stage.stage_id,
                    unknown=unknown,
                )
        self._assert_acyclic()

    def _assert_acyclic(self) -> None:
        colour: dict[str, int] = {}

        def visit(stage_id: str, path: tuple[str, ...]) -> None:
            state = colour.get(stage_id, 0)
            if state == 1:
                cycle = " -> ".join([*path, stage_id])
                raise StageGraphError(f"cycle in stage graph: {cycle}", cycle=cycle)
            if state == 2:
                return
            colour[stage_id] = 1
            for dep in self.definition(stage_id).depends_on:
                visit(dep, (*path, stage_id))
            colour[stage_id] = 2

        for stage in self.stages:
            visit(stage.stage_id, ())

    def definition(self, stage_id: str) -> StageDefinition:
        for stage in self.stages:
            if stage.stage_id == stage_id:
                return stage
        raise StageGraphError(f"unknown stage {stage_id!r}", stage_id=stage_id)

    def stage_ids(self) -> tuple[str, ...]:
        return tuple(s.stage_id for s in self.stages)

    def dependants_of(self, stage_id: str) -> tuple[str, ...]:
        """Every stage that transitively depends on *stage_id*."""
        found: set[str] = set()
        frontier = [stage_id]
        while frontier:
            current = frontier.pop()
            for stage in self.stages:
                if current in stage.depends_on and stage.stage_id not in found:
                    found.add(stage.stage_id)
                    frontier.append(stage.stage_id)
        return tuple(sorted(found))


def linear_graph(stage_ids: Iterable[str], *, mutating: Iterable[str] = ()) -> StageGraph:
    """Build the chain `a -> b -> c` for a sequential pipeline.

    MUSAEUS's pipelines are ordered lists today, so their real dependency
    structure is a chain. Stating that explicitly is what lets a
    prerequisite failure block the right set of stages; an ordered list
    alone carries the order but not the *requirement*, which is why a
    failure in the middle of one currently just moves on to the next."""
    mutating_set = set(mutating)
    ids = tuple(stage_ids)
    return StageGraph(
        tuple(
            StageDefinition(
                stage_id=stage_id,
                depends_on=() if index == 0 else (ids[index - 1],),
                mutates=stage_id in mutating_set,
            )
            for index, stage_id in enumerate(ids)
        )
    )


# ── Gating ────────────────────────────────────────────────────────────────────

REASON_PREREQUISITE_NOT_SUCCEEDED = "prerequisite_not_succeeded"
REASON_PREREQUISITE_ABSENT = "prerequisite_absent"
REASON_PREREQUISITE_DIGEST_MISMATCH = "prerequisite_digest_mismatch"
REASON_RUN_BLOCKED = "run_blocked_by_unmapped_evidence"
REASON_RUN_CANCELLED = "run_cancellation_requested"


@dataclass(frozen=True)
class Blocker:
    """A typed blocker, shaped for a `stage.blocked` payload."""

    stage_id: str
    status: str
    reason_code: str

    def as_payload(self) -> dict[str, str]:
        return {
            "stage_id": self.stage_id,
            "status": self.status,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class StageDecision:
    stage_id: str
    eligible: bool
    blockers: tuple[Blocker, ...] = ()
    recovery_action: str | None = None

    def blocker_payload(self) -> list[dict[str, str]]:
        return [b.as_payload() for b in self.blockers]


def _latest_attempt(state: ProjectedState, run_id: str, stage_id: str) -> StageProjection | None:
    """The highest-numbered attempt recorded for this stage.

    Deliberately the latest, not the best. A stage that succeeded on
    attempt 1 and failed on attempt 2 is failed; picking whichever attempt
    reads most favourably is how a failure gets resumed past."""
    candidates = [
        stage for (r, s, _), stage in state.stages.items() if r == run_id and s == stage_id
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.attempt)


def evaluate_gating(
    graph: StageGraph,
    state: ProjectedState,
    run_id: str,
    *,
    expected_input_digests: Mapping[str, str] | None = None,
) -> dict[str, StageDecision]:
    """
    Decide, for every stage in *graph*, whether its prerequisites permit it
    to start.

    `expected_input_digests` maps a prerequisite stage_id to the output
    digest the caller expects it to still have. A recorded success whose
    digest no longer matches is treated as invalid, not as success --
    DR-03's "succeeded -> blocked when recorded inputs/outputs are
    invalidated".
    """
    digests = dict(expected_input_digests or {})
    run = state.runs.get(run_id)
    decisions: dict[str, StageDecision] = {}

    for stage in graph.stages:
        blockers: list[Blocker] = []

        if run is not None and run.status == BLOCKED:
            blockers.append(Blocker(run_id, BLOCKED, REASON_RUN_BLOCKED))

        # MCR-005: once cancellation is requested, no new stage starts.
        # Enforced here rather than in each stage, so a stage cannot be
        # written that forgets to ask.
        if run is not None and run.cancellation_requested:
            blockers.append(Blocker(run_id, CANCELLED, REASON_RUN_CANCELLED))

        for dependency in stage.depends_on:
            recorded = _latest_attempt(state, run_id, dependency)
            if recorded is None:
                blockers.append(Blocker(dependency, PENDING, REASON_PREREQUISITE_ABSENT))
                continue
            if recorded.status != SUCCEEDED:
                blockers.append(
                    Blocker(dependency, recorded.status, REASON_PREREQUISITE_NOT_SUCCEEDED)
                )
                continue
            expected = digests.get(dependency)
            if expected is not None and recorded.output_digest != expected:
                blockers.append(Blocker(dependency, SUCCEEDED, REASON_PREREQUISITE_DIGEST_MISMATCH))

        decisions[stage.stage_id] = StageDecision(
            stage_id=stage.stage_id,
            eligible=not blockers,
            blockers=tuple(blockers),
            recovery_action=_recovery_action(tuple(blockers)) if blockers else None,
        )
    return decisions


def _recovery_action(blockers: tuple[Blocker, ...]) -> str:
    """A named, actionable next step -- not a restatement of the problem."""
    first = blockers[0]
    if first.reason_code == REASON_RUN_CANCELLED:
        return (
            "cancellation has been requested for this run; complete the recovery path "
            "and record a terminal outcome -- do not start further work"
        )
    if first.reason_code == REASON_RUN_BLOCKED:
        return (
            "resolve the run's unmapped legacy evidence, or rebuild state from the "
            "pre-reset database snapshot; this run cannot be resumed"
        )
    if first.reason_code == REASON_PREREQUISITE_ABSENT:
        return f"run {first.stage_id} first; it has no recorded attempt"
    if first.reason_code == REASON_PREREQUISITE_DIGEST_MISMATCH:
        return (
            f"re-run {first.stage_id}: it succeeded previously but its recorded output "
            f"digest no longer matches the current input"
        )
    return f"resolve {first.stage_id} (currently {first.status}), then retry as a new attempt"


# ── Resume ────────────────────────────────────────────────────────────────────

SKIP = "skip"
RERUN = "rerun"
BLOCK = "block"


@dataclass(frozen=True)
class ResumeDecision:
    stage_id: str
    action: str
    reason_code: str
    blockers: tuple[Blocker, ...] = ()
    recovery_action: str | None = None


@dataclass(frozen=True)
class ResumePlan:
    run_id: str
    decisions: tuple[ResumeDecision, ...] = field(default_factory=tuple)

    def by_action(self, action: str) -> tuple[str, ...]:
        return tuple(d.stage_id for d in self.decisions if d.action == action)

    def decision_for(self, stage_id: str) -> ResumeDecision:
        for decision in self.decisions:
            if decision.stage_id == stage_id:
                return decision
        raise KeyError(stage_id)


REASON_VALID_SUCCESS = "recorded_success_still_valid"
REASON_NO_RECORD = "no_recorded_attempt"
REASON_NOT_SUCCEEDED = "recorded_attempt_not_succeeded"
REASON_MISSING_OUTPUT_DIGEST = "recorded_success_has_no_output_digest"
REASON_INPUT_DIGEST_CHANGED = "input_digest_changed"
REASON_CONFIG_DIGEST_CHANGED = "config_digest_changed"


def plan_resume(
    graph: StageGraph,
    state: ProjectedState,
    run_id: str,
    *,
    current_input_digests: Mapping[str, str] | None = None,
    current_config_digest: str | None = None,
) -> ResumePlan:
    """
    Decide, per stage, whether resume may skip it.

    A stage is skipped only when ALL of these hold:
      * its latest recorded attempt is `succeeded`;
      * that attempt recorded an output digest (a success with no output
        digest recorded nothing verifiable, so it cannot be validated);
      * the current input digest matches the recorded one;
      * the run's configuration digest is unchanged.

    Anything else is `rerun`, or `block` when a prerequisite forbids even
    that. There is deliberately no flag that turns a `block` into a
    `skip`: DR-03 is explicit that no `--resume` may coerce a blocked
    stage to success.
    """
    inputs = dict(current_input_digests or {})
    run = state.runs.get(run_id)
    gating = evaluate_gating(
        graph, state, run_id, expected_input_digests=_recorded_outputs(state, run_id, graph)
    )
    decisions: list[ResumeDecision] = []

    config_changed = (
        current_config_digest is not None
        and run is not None
        and run.config_digest is not None
        and run.config_digest != current_config_digest
    )

    for stage in graph.stages:
        recorded = _latest_attempt(state, run_id, stage.stage_id)
        gate = gating[stage.stage_id]

        if recorded is not None and recorded.status == SUCCEEDED:
            if config_changed:
                decisions.append(
                    ResumeDecision(stage.stage_id, RERUN, REASON_CONFIG_DIGEST_CHANGED)
                )
                continue
            if recorded.output_digest is None:
                decisions.append(
                    ResumeDecision(stage.stage_id, RERUN, REASON_MISSING_OUTPUT_DIGEST)
                )
                continue
            expected_input = inputs.get(stage.stage_id)
            if expected_input is not None and recorded.input_digest != expected_input:
                decisions.append(ResumeDecision(stage.stage_id, RERUN, REASON_INPUT_DIGEST_CHANGED))
                continue
            decisions.append(ResumeDecision(stage.stage_id, SKIP, REASON_VALID_SUCCESS))
            continue

        # Not a valid success. It will be re-run -- if its prerequisites allow.
        reason = REASON_NO_RECORD if recorded is None else REASON_NOT_SUCCEEDED
        if not gate.eligible:
            decisions.append(
                ResumeDecision(
                    stage.stage_id,
                    BLOCK,
                    reason,
                    blockers=gate.blockers,
                    recovery_action=gate.recovery_action,
                )
            )
        else:
            decisions.append(ResumeDecision(stage.stage_id, RERUN, reason))

    return ResumePlan(run_id=run_id, decisions=tuple(decisions))


def _recorded_outputs(state: ProjectedState, run_id: str, graph: StageGraph) -> dict[str, str]:
    """Each stage's recorded output digest, for prerequisite comparison."""
    outputs: dict[str, str] = {}
    for stage_id in graph.stage_ids():
        recorded = _latest_attempt(state, run_id, stage_id)
        if recorded is not None and recorded.output_digest is not None:
            outputs[stage_id] = recorded.output_digest
    return outputs


def next_attempt_number(state: ProjectedState, run_id: str, stage_id: str) -> int:
    """The attempt number a re-run must record.

    Always one past the highest seen, never a reuse. Reusing the number
    would overwrite the record of the attempt that failed."""
    recorded = _latest_attempt(state, run_id, stage_id)
    return 1 if recorded is None else recorded.attempt + 1


def blocked_stage_payload(decision: StageDecision, attempt: int) -> dict[str, Any]:
    """Build a valid `stage.blocked` payload from a gating decision."""
    return {
        "stage_id": decision.stage_id,
        "attempt": attempt,
        "blockers": decision.blocker_payload(),
        "recovery_action": decision.recovery_action,
    }
