"""
P0-08 — lifecycle transitions, prerequisite gating, safe resume.

The headline assertion, stated by MCR-005 and required explicitly by the
task: **a failed stage is not skipped by resume**, and a digest mismatch
prevents a skip. `tests/test_p0_01_characterization.py` still carries the
regression showing the current resume path records a failed stage as
complete, so this is a live defect being closed, not a hypothetical.
"""

from __future__ import annotations

import pytest

from musaeus.state.events import (
    RUN_CREATED,
    STAGE_FAILED,
    STAGE_STARTED,
    STAGE_SUCCEEDED,
    adapt_legacy_event,
    new_event,
)
from musaeus.state.projector import (
    BLOCKED,
    CANCELLED,
    FAILED,
    PENDING,
    RUNNING,
    SUCCEEDED,
    project,
)
from musaeus.state.run_state import (
    BLOCK,
    REASON_CONFIG_DIGEST_CHANGED,
    REASON_INPUT_DIGEST_CHANGED,
    REASON_MISSING_OUTPUT_DIGEST,
    REASON_NOT_SUCCEEDED,
    REASON_PREREQUISITE_ABSENT,
    REASON_PREREQUISITE_DIGEST_MISMATCH,
    REASON_PREREQUISITE_NOT_SUCCEEDED,
    REASON_VALID_SUCCESS,
    RERUN,
    SKIP,
    StageDefinition,
    StageGraph,
    StageGraphError,
    TransitionError,
    blocked_stage_payload,
    check_transition,
    evaluate_gating,
    linear_graph,
    next_attempt_number,
    plan_resume,
)
from tests.test_p0_07_canonical_events import _legacy_row

PIPELINE = ("IngestStage", "ForgeStage", "FinalizeStage")


def _graph() -> StageGraph:
    return linear_graph(PIPELINE, mutating=("ForgeStage", "FinalizeStage"))


def _run_created(run_id: str = "run-A", config_digest: str = "cfg-1"):
    return new_event(
        run_id,
        0,
        RUN_CREATED,
        {
            "mode": "execute",
            "config_digest": config_digest,
            "scope_summary": {},
            "authority": "granted",
        },
    )


def _succeeded(run_id, seq, stage_id, attempt=1, in_d="in", out_d="out"):
    return new_event(
        run_id,
        seq,
        STAGE_SUCCEEDED,
        {
            "stage_id": stage_id,
            "attempt": attempt,
            "input_digest": in_d,
            "output_digest": out_d,
            "counts": {},
        },
        stage_id=stage_id,
        attempt=attempt,
    )


def _failed(run_id, seq, stage_id, attempt=1):
    return new_event(
        run_id,
        seq,
        STAGE_FAILED,
        {
            "stage_id": stage_id,
            "attempt": attempt,
            "error_code": "stage_failed",
            "safe_to_retry": True,
            "checkpoint_id": None,
        },
        stage_id=stage_id,
        attempt=attempt,
    )


# ── Transitions ───────────────────────────────────────────────────────────────


class TestTransitions:
    @pytest.mark.parametrize(
        "current, target",
        [
            (PENDING, RUNNING),
            (PENDING, BLOCKED),
            (RUNNING, SUCCEEDED),
            (RUNNING, FAILED),
            (RUNNING, CANCELLED),
        ],
    )
    def test_permitted_transitions_are_allowed(self, current, target):
        check_transition(current, target)

    @pytest.mark.parametrize(
        "current, target",
        [
            (PENDING, SUCCEEDED),
            (FAILED, SUCCEEDED),
            (BLOCKED, SUCCEEDED),
            (CANCELLED, SUCCEEDED),
            (SUCCEEDED, RUNNING),
            (BLOCKED, RUNNING),
        ],
    )
    def test_forbidden_transitions_are_refused(self, current, target):
        with pytest.raises(TransitionError):
            check_transition(current, target)

    def test_a_terminal_stage_returns_to_pending_only_as_a_new_attempt(self):
        for terminal in (FAILED, CANCELLED, BLOCKED):
            with pytest.raises(TransitionError) as exc:
                check_transition(terminal, PENDING)
            assert "new recorded attempt" in str(exc.value)
            check_transition(terminal, PENDING, new_attempt=True)

    def test_succeeded_becomes_blocked_only_when_invalidated(self):
        with pytest.raises(TransitionError) as exc:
            check_transition(SUCCEEDED, BLOCKED)
        assert "invalidated" in str(exc.value)
        check_transition(SUCCEEDED, BLOCKED, invalidated=True)

    def test_unknown_state_names_are_refused(self):
        with pytest.raises(TransitionError):
            check_transition("finished", SUCCEEDED)
        with pytest.raises(TransitionError):
            check_transition(RUNNING, "done")


# ── Graph ─────────────────────────────────────────────────────────────────────


class TestStageGraph:
    def test_linear_pipeline_becomes_a_chain(self):
        graph = _graph()
        assert graph.definition("IngestStage").depends_on == ()
        assert graph.definition("ForgeStage").depends_on == ("IngestStage",)
        assert graph.definition("FinalizeStage").depends_on == ("ForgeStage",)

    def test_dependants_are_transitive(self):
        graph = _graph()
        assert graph.dependants_of("IngestStage") == ("FinalizeStage", "ForgeStage")
        assert graph.dependants_of("FinalizeStage") == ()

    def test_unknown_dependency_is_refused(self):
        with pytest.raises(StageGraphError) as exc:
            StageGraph((StageDefinition("A", depends_on=("Nope",)),))
        assert "unknown stage" in str(exc.value)

    def test_cycle_is_refused(self):
        with pytest.raises(StageGraphError) as exc:
            StageGraph(
                (
                    StageDefinition("A", depends_on=("C",)),
                    StageDefinition("B", depends_on=("A",)),
                    StageDefinition("C", depends_on=("B",)),
                )
            )
        assert "cycle" in str(exc.value)

    def test_diamond_graph_is_accepted(self):
        """Negative control: the cycle detector must not reject a DAG that
        merely revisits a node by two paths."""
        graph = StageGraph(
            (
                StageDefinition("A"),
                StageDefinition("B", depends_on=("A",)),
                StageDefinition("C", depends_on=("A",)),
                StageDefinition("D", depends_on=("B", "C")),
            )
        )
        assert graph.dependants_of("A") == ("B", "C", "D")


# ── Gating ────────────────────────────────────────────────────────────────────


class TestPrerequisiteGating:
    def test_a_failed_prerequisite_blocks_every_dependant_with_a_named_action(self):
        state = project(
            (
                _run_created(),
                _succeeded("run-A", 1, "IngestStage"),
                _failed("run-A", 2, "ForgeStage"),
            )
        )
        decisions = evaluate_gating(_graph(), state, "run-A")

        assert decisions["IngestStage"].eligible is True
        assert decisions["ForgeStage"].eligible is True  # its own prerequisite succeeded

        finalize = decisions["FinalizeStage"]
        assert finalize.eligible is False
        assert finalize.blockers[0].stage_id == "ForgeStage"
        assert finalize.blockers[0].status == FAILED
        assert finalize.blockers[0].reason_code == REASON_PREREQUISITE_NOT_SUCCEEDED
        assert finalize.recovery_action is not None
        assert "ForgeStage" in finalize.recovery_action

    def test_an_absent_prerequisite_blocks_and_says_so(self):
        state = project((_run_created(),))
        decisions = evaluate_gating(_graph(), state, "run-A")
        forge = decisions["ForgeStage"]
        assert forge.eligible is False
        assert forge.blockers[0].reason_code == REASON_PREREQUISITE_ABSENT
        assert "has no recorded attempt" in forge.recovery_action

    def test_digest_mismatch_invalidates_a_recorded_success(self):
        state = project((_run_created(), _succeeded("run-A", 1, "IngestStage", out_d="out-1")))
        decisions = evaluate_gating(
            _graph(), state, "run-A", expected_input_digests={"IngestStage": "out-DIFFERENT"}
        )
        forge = decisions["ForgeStage"]
        assert forge.eligible is False
        assert forge.blockers[0].reason_code == REASON_PREREQUISITE_DIGEST_MISMATCH
        assert "no longer matches" in forge.recovery_action

    def test_matching_digest_does_not_block(self):
        """Negative control for the digest rule."""
        state = project((_run_created(), _succeeded("run-A", 1, "IngestStage", out_d="out-1")))
        decisions = evaluate_gating(
            _graph(), state, "run-A", expected_input_digests={"IngestStage": "out-1"}
        )
        assert decisions["ForgeStage"].eligible is True

    def test_the_latest_attempt_decides_not_the_best_one(self):
        """A stage that succeeded on attempt 1 and failed on attempt 2 is
        failed. Picking whichever attempt reads best is how a failure gets
        resumed past."""
        state = project(
            (
                _run_created(),
                _succeeded("run-A", 1, "IngestStage", attempt=1),
                _failed("run-A", 2, "IngestStage", attempt=2),
            )
        )
        decisions = evaluate_gating(_graph(), state, "run-A")
        assert decisions["ForgeStage"].eligible is False
        assert decisions["ForgeStage"].blockers[0].status == FAILED

    def test_a_run_blocked_by_unmapped_evidence_blocks_every_stage(self):
        state = project(
            (
                _run_created(),
                adapt_legacy_event(_legacy_row("HASH_COMPUTED", run_id="run-A"), 1),
            )
        )
        decisions = evaluate_gating(_graph(), state, "run-A")
        assert all(not d.eligible for d in decisions.values())
        assert "cannot be resumed" in decisions["IngestStage"].recovery_action

    def test_blocked_payload_is_a_valid_canonical_event(self):
        """The gating decision must be directly expressible as a
        stage.blocked event -- blockers as typed objects, not prose."""
        state = project((_run_created(), _failed("run-A", 1, "IngestStage")))
        decision = evaluate_gating(_graph(), state, "run-A")["ForgeStage"]
        payload = blocked_stage_payload(decision, attempt=1)
        event = new_event("run-A", 9, "stage.blocked", payload)
        assert event.payload["blockers"][0]["stage_id"] == "IngestStage"
        assert event.payload["recovery_action"]


# ── Resume ────────────────────────────────────────────────────────────────────


class TestResumeEligibility:
    def test_a_failed_stage_is_never_skipped(self):
        """The headline requirement."""
        state = project(
            (
                _run_created(),
                _succeeded("run-A", 1, "IngestStage"),
                _failed("run-A", 2, "ForgeStage"),
            )
        )
        plan = plan_resume(_graph(), state, "run-A")

        assert plan.decision_for("IngestStage").action == SKIP
        forge = plan.decision_for("ForgeStage")
        assert forge.action == RERUN
        assert forge.reason_code == REASON_NOT_SUCCEEDED
        assert "ForgeStage" not in plan.by_action(SKIP)

    def test_a_dependant_of_a_failed_stage_is_blocked_not_rerun(self):
        state = project(
            (
                _run_created(),
                _succeeded("run-A", 1, "IngestStage"),
                _failed("run-A", 2, "ForgeStage"),
            )
        )
        plan = plan_resume(_graph(), state, "run-A")
        finalize = plan.decision_for("FinalizeStage")
        assert finalize.action == BLOCK
        assert finalize.blockers[0].stage_id == "ForgeStage"
        assert finalize.recovery_action is not None

    def test_a_valid_success_is_skipped(self):
        """Negative control: resume must actually be able to skip, or it is
        just a re-run."""
        state = project(
            (
                _run_created(),
                _succeeded("run-A", 1, "IngestStage"),
                _succeeded("run-A", 2, "ForgeStage"),
            )
        )
        plan = plan_resume(_graph(), state, "run-A")
        assert plan.decision_for("IngestStage").action == SKIP
        assert plan.decision_for("IngestStage").reason_code == REASON_VALID_SUCCESS
        assert plan.decision_for("ForgeStage").action == SKIP

    def test_changed_input_digest_prevents_a_skip(self):
        state = project((_run_created(), _succeeded("run-A", 1, "IngestStage", in_d="in-1")))
        plan = plan_resume(
            _graph(), state, "run-A", current_input_digests={"IngestStage": "in-CHANGED"}
        )
        decision = plan.decision_for("IngestStage")
        assert decision.action == RERUN
        assert decision.reason_code == REASON_INPUT_DIGEST_CHANGED

    def test_unchanged_input_digest_still_skips(self):
        state = project((_run_created(), _succeeded("run-A", 1, "IngestStage", in_d="in-1")))
        plan = plan_resume(_graph(), state, "run-A", current_input_digests={"IngestStage": "in-1"})
        assert plan.decision_for("IngestStage").action == SKIP

    def test_a_success_with_no_output_digest_cannot_be_validated_so_is_rerun(self):
        state = project((_run_created(), _succeeded("run-A", 1, "IngestStage", out_d=None)))
        decision = plan_resume(_graph(), state, "run-A").decision_for("IngestStage")
        assert decision.action == RERUN
        assert decision.reason_code == REASON_MISSING_OUTPUT_DIGEST

    def test_changed_configuration_prevents_every_skip(self):
        state = project(
            (
                _run_created(config_digest="cfg-1"),
                _succeeded("run-A", 1, "IngestStage"),
                _succeeded("run-A", 2, "ForgeStage"),
            )
        )
        plan = plan_resume(_graph(), state, "run-A", current_config_digest="cfg-2")
        assert plan.by_action(SKIP) == ()
        assert plan.decision_for("IngestStage").reason_code == REASON_CONFIG_DIGEST_CHANGED

    def test_unchanged_configuration_still_skips(self):
        state = project(
            (_run_created(config_digest="cfg-1"), _succeeded("run-A", 1, "IngestStage"))
        )
        plan = plan_resume(_graph(), state, "run-A", current_config_digest="cfg-1")
        assert plan.decision_for("IngestStage").action == SKIP

    def test_a_cancelled_stage_is_not_skipped(self):
        state = project(
            (
                _run_created(),
                new_event(
                    "run-A",
                    1,
                    "stage.cancelled",
                    {
                        "stage_id": "IngestStage",
                        "attempt": 1,
                        "safe_checkpoint": True,
                        "checkpoint_id": "ckpt",
                    },
                ),
            )
        )
        assert plan_resume(_graph(), state, "run-A").decision_for("IngestStage").action == RERUN

    def test_a_started_but_never_finished_stage_is_not_skipped(self):
        """The OOM case: a run killed mid-stage leaves `started` with no
        terminal event. That is not a success."""
        state = project(
            (
                _run_created(),
                new_event(
                    "run-A",
                    1,
                    STAGE_STARTED,
                    {"stage_id": "IngestStage", "attempt": 1, "input_digest": "in"},
                ),
            )
        )
        decision = plan_resume(_graph(), state, "run-A").decision_for("IngestStage")
        assert decision.action == RERUN
        assert decision.reason_code == REASON_NOT_SUCCEEDED

    def test_no_flag_can_coerce_a_blocked_stage_to_skip(self):
        """DR-03: no `--resume` may coerce a blocked stage to success. The
        strongest form of that guarantee is that plan_resume takes no
        parameter which could."""
        import inspect

        params = set(inspect.signature(plan_resume).parameters)
        assert params == {
            "graph",
            "state",
            "run_id",
            "current_input_digests",
            "current_config_digest",
        }, "a new parameter here must not be an override switch"

    def test_next_attempt_never_reuses_a_number(self):
        state = project(
            (
                _run_created(),
                _succeeded("run-A", 1, "IngestStage", attempt=1),
                _failed("run-A", 2, "IngestStage", attempt=2),
            )
        )
        assert next_attempt_number(state, "run-A", "IngestStage") == 3
        assert next_attempt_number(state, "run-A", "ForgeStage") == 1
