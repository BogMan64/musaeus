"""
P0-07 — canonical events, one projector, rebuild parity.

The acceptance criterion this file exists to prove (MCR-004): "rebuilding
from a canonical event sequence produces state equivalent to live
execution for the same sequence, including event names, payload meaning,
and terminal stage status."

Parity is asserted field by field, never by row count. `rebuild.py` was
disabled precisely because a row-count-shaped confidence was available
for a rebuild that produced 27,000 metadata-less PENDING stubs.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from musaeus.state.events import (
    LEGACY_LIFECYCLE_TYPES,
    LEGACY_UNMAPPED,
    REQUIRED_PAYLOAD_FIELDS,
    RUN_CREATED,
    RUN_TERMINAL,
    STAGE_BLOCKED,
    STAGE_FAILED,
    STAGE_STARTED,
    STAGE_SUCCEEDED,
    CanonicalEvent,
    EventSequenceError,
    EventValidationError,
    adapt_legacy_event,
    append_event,
    new_event,
    read_events,
)
from musaeus.state.migrator import migrate
from musaeus.state.projector import (
    BLOCKED,
    FAILED,
    RUNNING,
    SUCCEEDED,
    ProjectedState,
    RebuildBlockedError,
    apply_event,
    assert_rebuildable,
    project,
    projection_parity,
    rebuild_projection,
    write_projection,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def migrated_db(disposable_vault) -> Path:
    """A disposable database carried to the current schema, so the
    canonical tables exist because a real migration created them."""
    conn = disposable_vault.open_db()
    conn.close()
    migrate(disposable_vault.cfg.db_path, recovery_root=disposable_vault.recovery_root)
    return disposable_vault.cfg.db_path


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _canonical_sequence(run_id: str = "run-A") -> tuple[CanonicalEvent, ...]:
    """One representative run: created, two stages, one of which fails and
    blocks a dependant, then a terminal event."""
    return (
        new_event(
            run_id,
            0,
            RUN_CREATED,
            {
                "mode": "execute",
                "config_digest": "cfg-abc",
                "scope_summary": {"root": "fixture"},
                "authority": "granted",
            },
            scope_id="scope-1",
        ),
        new_event(
            run_id,
            1,
            STAGE_STARTED,
            {"stage_id": "IngestStage", "attempt": 1, "input_digest": "in-1"},
            stage_id="IngestStage",
            attempt=1,
        ),
        new_event(
            run_id,
            2,
            STAGE_SUCCEEDED,
            {
                "stage_id": "IngestStage",
                "attempt": 1,
                "input_digest": "in-1",
                "output_digest": "out-1",
                "counts": {"files": 3},
            },
            stage_id="IngestStage",
            attempt=1,
        ),
        new_event(
            run_id,
            3,
            STAGE_FAILED,
            {
                "stage_id": "ForgeStage",
                "attempt": 1,
                "error_code": "stage_failed",
                "safe_to_retry": True,
                "checkpoint_id": "ckpt-1",
            },
            stage_id="ForgeStage",
            attempt=1,
        ),
        new_event(
            run_id,
            4,
            STAGE_BLOCKED,
            {
                "stage_id": "FinalizeStage",
                "attempt": 1,
                "blockers": [{"stage_id": "ForgeStage", "status": "failed"}],
                "recovery_action": "retry ForgeStage",
            },
            stage_id="FinalizeStage",
            attempt=1,
        ),
        new_event(
            run_id,
            5,
            RUN_TERMINAL,
            {
                "status": "failed",
                "exit_code": 3,
                "reason_code": "stage_failed",
                "stage_counts": {"succeeded": 1, "failed": 1, "blocked": 1},
                "checkpoint_id": "ckpt-1",
                "rollback_status": "not_required",
            },
        ),
    )


# ── Vocabulary and validation ─────────────────────────────────────────────────


class TestVocabularyIsClosed:
    def test_unknown_event_type_is_refused(self):
        with pytest.raises(EventValidationError) as exc:
            new_event("run-A", 0, "stage.probably_fine", {})
        assert "closed canonical vocabulary" in str(exc.value)

    def test_every_declared_type_has_required_fields(self):
        for event_type, required in REQUIRED_PAYLOAD_FIELDS.items():
            assert isinstance(required, frozenset), event_type

    def test_missing_required_payload_field_is_refused(self):
        with pytest.raises(EventValidationError) as exc:
            new_event("run-A", 0, RUN_CREATED, {"mode": "execute", "authority": "granted"})
        assert "config_digest" in str(exc.value)
        assert "scope_summary" in str(exc.value)

    def test_complete_payload_is_accepted(self):
        """Negative control: the validator must be able to say yes."""
        event = new_event(
            "run-A",
            0,
            RUN_CREATED,
            {
                "mode": "preview",
                "config_digest": "d",
                "scope_summary": {},
                "authority": "none",
            },
        )
        assert event.event_type == RUN_CREATED

    @pytest.mark.parametrize("bad", ["a blocking problem", {"stage": "x"}, 7])
    def test_typed_array_fields_reject_prose(self, bad):
        """DR-02: checks/dependencies/blockers are arrays of typed objects,
        not prose. A sentence here reads fine in a report and cannot be
        acted on by code."""
        with pytest.raises(EventValidationError) as exc:
            new_event(
                "run-A",
                0,
                STAGE_BLOCKED,
                {
                    "stage_id": "S",
                    "attempt": 1,
                    "blockers": bad,
                    "recovery_action": "retry",
                },
            )
        assert "blockers" in str(exc.value)

    def test_typed_array_field_rejects_a_list_of_strings(self):
        with pytest.raises(EventValidationError) as exc:
            new_event(
                "run-A",
                0,
                STAGE_BLOCKED,
                {
                    "stage_id": "S",
                    "attempt": 1,
                    "blockers": ["ForgeStage failed"],
                    "recovery_action": "retry",
                },
            )
        assert "must be an object" in str(exc.value)

    @pytest.mark.parametrize(
        "key", ["api_key", "LASTFM_API_KEY", "password", "auth_token", "client_secret"]
    )
    def test_credential_bearing_payload_keys_are_refused(self, key):
        with pytest.raises(EventValidationError) as exc:
            new_event(
                "run-A",
                0,
                RUN_CREATED,
                {
                    "mode": "execute",
                    "config_digest": "d",
                    "scope_summary": {"provider": {key: "xyz"}},
                    "authority": "granted",
                },
            )
        assert "credential" in str(exc.value).lower()

    def test_nested_payload_without_credentials_is_accepted(self):
        """Negative control for the denylist: ordinary nested config must
        still be allowed through."""
        event = new_event(
            "run-A",
            0,
            RUN_CREATED,
            {
                "mode": "execute",
                "config_digest": "d",
                "scope_summary": {"provider": {"name": "lastfm", "enabled": False}},
                "authority": "granted",
            },
        )
        assert event.payload["scope_summary"]["provider"]["name"] == "lastfm"


# ── Append rules ──────────────────────────────────────────────────────────────


class TestAppendRules:
    def test_events_are_persisted_and_read_back_in_sequence_order(self, migrated_db):
        conn = _connect(migrated_db)
        try:
            for event in _canonical_sequence():
                assert append_event(conn, event) is True
            read_back = read_events(conn, "run-A")
        finally:
            conn.close()
        assert [e.sequence for e in read_back] == [0, 1, 2, 3, 4, 5]
        assert read_back[0].payload["mode"] == "execute"

    def test_replaying_the_same_event_id_is_a_no_op(self, migrated_db):
        conn = _connect(migrated_db)
        try:
            event = _canonical_sequence()[0]
            assert append_event(conn, event) is True
            assert append_event(conn, event) is False, "replay must be idempotent, not an error"
            count = conn.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_two_different_events_cannot_share_a_sequence_slot(self, migrated_db):
        conn = _connect(migrated_db)
        try:
            first, second = _canonical_sequence()[0], _canonical_sequence()[0]
            append_event(conn, first)
            collision = CanonicalEvent(
                run_id=second.run_id,
                sequence=second.sequence,
                event_type=second.event_type,
                payload=second.payload,
                occurred_at=second.occurred_at,
            )
            with pytest.raises(EventSequenceError) as exc:
                append_event(conn, collision)
            assert "sequences are unique within a run" in str(exc.value)
        finally:
            conn.close()

    def test_out_of_order_append_is_refused(self, migrated_db):
        """Distinct from the slot-collision rule above: this targets a
        sequence that is FREE but below the run's high-water mark. Aiming
        at an occupied slot would trip the collision guard first and prove
        nothing about ordering -- which is what the first version of this
        test did."""
        conn = _connect(migrated_db)
        try:
            events = _canonical_sequence()
            append_event(conn, events[0])  # sequence 0
            append_event(conn, events[3])  # sequence 3, leaving 1 and 2 free
            late = new_event(
                "run-A",
                2,
                STAGE_STARTED,
                {"stage_id": "Late", "attempt": 1, "input_digest": None},
            )
            with pytest.raises(EventSequenceError) as exc:
                append_event(conn, late)
            assert "out of order" in str(exc.value)
            assert exc.value.details["high_water"] == 3
        finally:
            conn.close()

    def test_separate_runs_have_independent_sequences(self, migrated_db):
        conn = _connect(migrated_db)
        try:
            append_event(conn, _canonical_sequence("run-A")[0])
            append_event(conn, _canonical_sequence("run-B")[0])
            assert len(read_events(conn, "run-A")) == 1
            assert len(read_events(conn, "run-B")) == 1
        finally:
            conn.close()


# ── The projector ─────────────────────────────────────────────────────────────


class TestProjection:
    def test_terminal_states_are_projected_not_counted(self):
        state = project(_canonical_sequence())
        run = state.runs["run-A"]
        assert run.status == FAILED
        assert run.exit_code == 3
        assert run.reason_code == "stage_failed"
        assert run.checkpoint_id == "ckpt-1"

        assert state.stages[("run-A", "IngestStage", 1)].status == SUCCEEDED
        assert state.stages[("run-A", "IngestStage", 1)].output_digest == "out-1"
        failed = state.stages[("run-A", "ForgeStage", 1)]
        assert failed.status == FAILED
        assert failed.safe_to_retry is True
        blocked = state.stages[("run-A", "FinalizeStage", 1)]
        assert blocked.status == BLOCKED
        assert blocked.recovery_action == "retry ForgeStage"
        assert json.loads(blocked.blockers[0])["stage_id"] == "ForgeStage"

    def test_a_failed_stage_is_never_projected_as_succeeded(self):
        """MCR-004's third acceptance criterion, stated directly."""
        state = project(_canonical_sequence())
        assert state.stages[("run-A", "ForgeStage", 1)].status != SUCCEEDED
        assert state.runs["run-A"].status != SUCCEEDED

    def test_apply_event_is_pure(self):
        events = _canonical_sequence()
        start = ProjectedState.empty()
        first = apply_event(start, events[0])
        assert start.runs == {}, "the input state must not be mutated"
        assert first.runs["run-A"].status == RUNNING

    def test_project_is_a_fold_of_apply_event(self):
        """The structural guarantee behind parity: there is one transition
        function, and `project` is nothing but repeated application of it.
        If these ever disagree, two implementations have appeared."""
        events = _canonical_sequence()
        folded = ProjectedState.empty()
        for event in events:
            folded = apply_event(folded, event)
        assert projection_parity(folded, project(events)) == []


# ── Rebuild parity ────────────────────────────────────────────────────────────


class TestRebuildParity:
    def test_live_and_rebuilt_projections_are_equivalent(self, migrated_db):
        """Live folds events one at a time as they are appended; rebuild
        reads the persisted sequence back and folds from empty. Same
        function, same order, and the result is compared field by field."""
        events = _canonical_sequence()

        live = ProjectedState.empty()
        conn = _connect(migrated_db)
        try:
            conn.execute("BEGIN")
            for event in events:
                append_event(conn, event)
                live = apply_event(live, event)
            write_projection(conn, live)
            conn.execute("COMMIT")

            rebuilt = rebuild_projection(conn)
        finally:
            conn.close()

        assert projection_parity(live, rebuilt) == []
        assert live.runs == rebuilt.runs
        assert live.stages == rebuilt.stages

    def test_parity_check_detects_a_terminal_status_difference(self, migrated_db):
        """Negative control. A parity function that cannot report a
        difference proves nothing when it reports none -- and a differing
        terminal status with identical row counts is exactly the failure
        rebuild.py's row counting could not see."""
        live = project(_canonical_sequence())
        divergent = project(
            (
                *_canonical_sequence()[:5],
                new_event(
                    "run-A",
                    5,
                    RUN_TERMINAL,
                    {
                        "status": "succeeded",
                        "exit_code": 0,
                        "reason_code": "ok",
                        "stage_counts": {},
                        "checkpoint_id": None,
                        "rollback_status": None,
                    },
                ),
            )
        )
        assert len(live.runs) == len(divergent.runs)
        assert len(live.stages) == len(divergent.stages)
        diffs = projection_parity(live, divergent)
        assert diffs, "row counts match; the parity check must still catch this"
        assert "run run-A differs" in diffs[0]

    def test_persisted_projection_matches_the_computed_one(self, migrated_db):
        events = _canonical_sequence()
        state = project(events)
        conn = _connect(migrated_db)
        try:
            conn.execute("BEGIN")
            for event in events:
                append_event(conn, event)
            write_projection(conn, state)
            conn.execute("COMMIT")

            run_row = conn.execute("SELECT * FROM projected_runs WHERE run_id = 'run-A'").fetchone()
            stage_rows = {
                (r["stage_id"], r["attempt"]): r["status"]
                for r in conn.execute("SELECT * FROM projected_stages")
            }
        finally:
            conn.close()

        assert run_row["status"] == FAILED
        assert run_row["exit_code"] == 3
        assert stage_rows[("IngestStage", 1)] == SUCCEEDED
        assert stage_rows[("ForgeStage", 1)] == FAILED
        assert stage_rows[("FinalizeStage", 1)] == BLOCKED

    def test_rewriting_the_projection_is_idempotent(self, migrated_db):
        events = _canonical_sequence()
        state = project(events)
        conn = _connect(migrated_db)
        try:
            conn.execute("BEGIN")
            for event in events:
                append_event(conn, event)
            write_projection(conn, state)
            write_projection(conn, state)
            conn.execute("COMMIT")
            runs = conn.execute("SELECT COUNT(*) FROM projected_runs").fetchone()[0]
            stages = conn.execute("SELECT COUNT(*) FROM projected_stages").fetchone()[0]
        finally:
            conn.close()
        assert (runs, stages) == (1, 3)


# ── Legacy adaptation and blocking ────────────────────────────────────────────


def _legacy_row(event_type: str, **over) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE e (run_id TEXT, ts TEXT, event_type TEXT, file_path TEXT, "
        "old_value TEXT, new_value TEXT, stage TEXT, note TEXT)"
    )
    values = {
        "run_id": "legacy-run",
        "ts": "2026-08-23T10:00:00Z",
        "event_type": event_type,
        "file_path": None,
        "old_value": None,
        "new_value": None,
        "stage": None,
        "note": None,
    }
    values.update(over)
    conn.execute(
        "INSERT INTO e VALUES (:run_id, :ts, :event_type, :file_path, :old_value, "
        ":new_value, :stage, :note)",
        values,
    )
    return conn.execute("SELECT * FROM e").fetchone()


class TestLegacyAdapter:
    def test_the_three_lifecycle_types_map_cleanly(self):
        assert {"RUN_START", "RUN_END", "STAGE_COMPLETE"} == LEGACY_LIFECYCLE_TYPES
        assert adapt_legacy_event(_legacy_row("RUN_START"), 0).event_type == RUN_CREATED
        assert adapt_legacy_event(_legacy_row("RUN_END"), 1).event_type == RUN_TERMINAL
        mapped = adapt_legacy_event(_legacy_row("STAGE_COMPLETE", stage="ForgeStage"), 2)
        assert mapped.event_type == STAGE_SUCCEEDED
        assert mapped.payload["stage_id"] == "ForgeStage"

    @pytest.mark.parametrize(
        "legacy_type", ["HASH_COMPUTED", "METADATA_EXTRACTED", "FORGE_TAG", "FINALIZE_MOVE"]
    )
    def test_lossy_content_events_become_unmapped_not_guessed(self, legacy_type):
        """These are the types rebuild.py proved cannot be reconstructed:
        truncated hashes, absent metadata columns. The adapter must not
        invent a success for them."""
        adapted = adapt_legacy_event(_legacy_row(legacy_type, new_value="0faef0355d05cb91..."), 0)
        assert adapted.event_type == LEGACY_UNMAPPED
        assert adapted.payload["legacy_type"] == legacy_type
        assert adapted.payload["reason_code"] == "legacy_payload_lossy"
        assert len(adapted.payload["legacy_payload_digest"]) == 64

    def test_a_never_seen_type_is_distinguished_from_a_known_lossy_one(self):
        adapted = adapt_legacy_event(_legacy_row("SOMETHING_NEW_IN_2027"), 0)
        assert adapted.payload["reason_code"] == "legacy_type_unknown"

    def test_digest_is_stable_for_identical_evidence(self):
        a = adapt_legacy_event(_legacy_row("FORGE_TAG", note="x"), 0)
        b = adapt_legacy_event(_legacy_row("FORGE_TAG", note="x"), 9)
        c = adapt_legacy_event(_legacy_row("FORGE_TAG", note="y"), 0)
        assert a.payload["legacy_payload_digest"] == b.payload["legacy_payload_digest"]
        assert a.payload["legacy_payload_digest"] != c.payload["legacy_payload_digest"]


class TestUnmappedEvidenceBlocksRebuild:
    def test_an_unmapped_event_blocks_the_whole_run(self):
        events = (
            *_canonical_sequence()[:3],
            adapt_legacy_event(_legacy_row("HASH_COMPUTED", run_id="run-A"), 3),
        )
        state = project(events)
        assert state.runs["run-A"].status == BLOCKED
        assert "could not be mapped" in state.runs["run-A"].blocked_reason
        assert state.blocked_runs() == ("run-A",)

        with pytest.raises(RebuildBlockedError) as exc:
            assert_rebuildable(state)
        assert exc.value.reason_code == "rebuild_blocked"
        assert "HASH_COMPUTED" in str(exc.value.details["legacy_types"])

    def test_a_clean_run_is_rebuildable(self):
        """Negative control: the block must be capable of not firing, or it
        is not a decision."""
        assert_rebuildable(project(_canonical_sequence()))

    def test_unmapped_evidence_is_preserved_not_dropped(self, migrated_db):
        events = (
            *_canonical_sequence()[:3],
            adapt_legacy_event(_legacy_row("METADATA_EXTRACTED", run_id="run-A"), 3),
        )
        state = project(events)
        conn = _connect(migrated_db)
        try:
            conn.execute("BEGIN")
            write_projection(conn, state)
            conn.execute("COMMIT")
            row = conn.execute("SELECT * FROM projected_unmapped").fetchone()
        finally:
            conn.close()
        assert row["legacy_type"] == "METADATA_EXTRACTED"
        assert row["reason_code"] == "legacy_payload_lossy"
        assert len(row["legacy_payload_digest"]) == 64
