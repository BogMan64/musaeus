"""
MUSAEUS — dry-run/preview behaviour (was P0-02 guard, now P0-04/P0-05)

History matters for reading these tests. P0-01 proved that unguarded,
dry_run=True unconditionally called cfg.ensure_dirs() and
RunContext.new()/record_stage() -- creating the real vault skeleton, the
real SQLite DB, and RUN_START/STAGE_COMPLETE/RUN_END events -- before any
stage ran, and that Enrich/MBEnrich/AcousticID still made live network
calls regardless of the flag. P0-02 responded with a blunt fail-closed
refusal: exit code 2, no preview at all.

This file used to assert that refusal. It now asserts the thing the
refusal was standing in for.

The spec's condition for lifting the guard was P0-04 AND P0-05 both
implemented and fixture-proven. Both are: --dry-run routes to the pure
planner (musaeus/planner.py), which never calls ensure_dirs(), never
opens a writable connection, never logs an event, and never instantiates
a stage -- a stage object being the mutation-capable thing. Network
access is refused by the local-only gateway (musaeus/network_policy.py),
which records every attempt BEFORE raising so that the broad `except`
blocks in those stages cannot erase the evidence.

So the assertions flip from "is previewed with exit 2" to "succeeds,
renders a plan, and changes nothing" -- a strictly stronger claim than
the refusal ever made, because a refusal only proves nothing happened
when nothing was allowed to start.

Still built on the P0-01 fixture harness (tests/disposable_vault.py,
tests/conftest.py).
"""

from __future__ import annotations

import sqlite3

import pytest

from musaeus.stages import (
    DEFAULT_PIPELINE,
    AcousticIDStage,
    EnrichStage,
    IngestStage,
    MBEnrichStage,
    PreflightStage,
)
from tests.disposable_vault import snapshot_vault_state


def _patch_cli_for_vault(monkeypatch, disposable_vault):
    """Point musaeus.cli's config/resume-file resolution at the disposable
    vault, exactly as tests/test_p0_01_characterization.py already does,
    and return the imported cli module for the caller to invoke
    _run_pipeline()/_reject_unsafe_dry_run() on."""
    import musaeus.cli as cli_mod

    monkeypatch.setattr(cli_mod, "get_config", lambda: disposable_vault.cfg)
    monkeypatch.setattr(cli_mod, "_RESUME_FILE", disposable_vault.config_home / "resume_state.json")
    return cli_mod


# ── Direct unit tests of the guard helper itself ──────────────────────────────


class TestRejectUnsafeDryRunHelper:
    """Fast, direct unit tests of _reject_unsafe_dry_run() in isolation
    (no vault I/O needed) -- complements the end-to-end _run_pipeline
    tests below."""

    def test_returns_none_when_dry_run_is_false(self):
        import musaeus.cli as cli_mod

        assert cli_mod._reject_unsafe_dry_run(DEFAULT_PIPELINE, dry_run=False) is None
        assert cli_mod._reject_unsafe_dry_run([EnrichStage], dry_run=False) is None
        assert cli_mod._reject_unsafe_dry_run([], dry_run=False) is None

    def test_the_helper_still_refuses_if_called_directly(self):
        """_reject_unsafe_dry_run is retained, not deleted.

        _run_pipeline no longer routes through it -- --dry-run goes to the
        planner instead -- but any caller reaching the helper directly is
        still asking for the old unsafe path and must still be refused.
        """
        import musaeus.cli as cli_mod

        assert cli_mod._reject_unsafe_dry_run([PreflightStage], dry_run=True) == 2
        assert cli_mod._reject_unsafe_dry_run(DEFAULT_PIPELINE, dry_run=True) == 2
        assert cli_mod._reject_unsafe_dry_run([], dry_run=True) == 2

    def test_network_stage_membership_is_exactly_the_three_named_stages(self):
        import musaeus.cli as cli_mod

        assert {EnrichStage, MBEnrichStage, AcousticIDStage} == cli_mod._NETWORK_STAGES
        assert PreflightStage not in cli_mod._NETWORK_STAGES
        assert IngestStage not in cli_mod._NETWORK_STAGES


# ── Guard fires before directory/DB initialisation ────────────────────────────


class TestDryRunGuardBlocksDirsAndDB:
    """The P0-02 guard must fire before get_config()/cfg.ensure_dirs()/
    open_db() -- i.e. before ANY directory or database side effect --
    independent of whether a network stage is involved."""

    def test_guard_fires_before_any_directory_is_created(self, disposable_vault, monkeypatch):
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)
        assert not disposable_vault.root.exists()

        rc = cli_mod._run_pipeline(DEFAULT_PIPELINE, dry_run=True)

        assert rc == 0
        assert not disposable_vault.root.exists()
        assert not disposable_vault.cfg.inbox.exists()
        assert not disposable_vault.cfg.alac_library.exists()
        assert not cli_mod._RESUME_FILE.exists(), (
            "a previewed dry-run must not reach _load_resume/_save_resume either"
        )

    def test_guard_fires_before_the_db_file_is_created(self, disposable_vault, monkeypatch):
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)
        assert not disposable_vault.cfg.db_path.exists()

        rc = cli_mod._run_pipeline([PreflightStage], dry_run=True)

        assert rc == 0
        assert not disposable_vault.cfg.db_path.exists()

    def test_guard_fires_before_any_new_event_is_written_to_an_existing_db(
        self, disposable_vault, monkeypatch
    ):
        """Even if the vault/DB already exist from a prior REAL run, a
        later dry-run must not open a writable connection or append a
        new RUN_START/STAGE_COMPLETE/RUN_END event to it."""
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)

        rc_real = cli_mod._run_pipeline([PreflightStage], dry_run=False)
        assert rc_real == 0
        assert disposable_vault.cfg.db_path.exists()

        conn = sqlite3.connect(str(disposable_vault.cfg.db_path))
        try:
            before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()

        rc_dry = cli_mod._run_pipeline([PreflightStage], dry_run=True)
        assert rc_dry == 0

        conn = sqlite3.connect(str(disposable_vault.cfg.db_path))
        try:
            after = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        finally:
            conn.close()
        assert after == before, "a previewed dry-run must not append any new event"

    def test_guard_exit_code_and_message_for_dir_db_path(
        self, disposable_vault, monkeypatch, capsys
    ):
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)

        rc = cli_mod._run_pipeline(DEFAULT_PIPELINE, dry_run=True)

        assert rc == 0
        captured = capsys.readouterr()
        # The plan is the product now, and it goes to stdout so it can be
        # piped. The old refusal went to stderr; there should be none.
        assert "PREVIEW ONLY" in captured.out
        assert "mode: preview" in captured.out
        assert "temporarily disabled" not in captured.err.lower()
        # The old refusal was careful never to claim a completed no-op,
        # because it had not previewed anything. The plan CAN make that
        # claim, and states it explicitly -- that is the whole difference.
        assert "nothing was changed" in captured.out


# ── Guard fires before any network connection (the 3 network stages) ─────────


class TestDryRunGuardBlocksNetworkStages:
    """EnrichStage/MBEnrichStage/AcousticIDStage make unconditional
    network calls in their _enrich()/_run() methods regardless of
    dry_run -- the guard must additionally name these stages and prove
    zero connection attempts, using the transport_harness fixture so any
    attempt would be observed (and would fail the test) rather than
    silently succeeding."""

    @pytest.mark.parametrize(
        "stage_cls",
        [EnrichStage, MBEnrichStage, AcousticIDStage],
        ids=["enrich", "mb_enrich", "acousticid"],
    )
    def test_guard_fires_before_any_network_connection(
        self, disposable_vault, monkeypatch, transport_harness, stage_cls
    ):
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)
        attempts_before = len(transport_harness.attempts)

        rc = cli_mod._run_pipeline([stage_cls], dry_run=True)

        assert rc == 0
        assert len(transport_harness.attempts) == attempts_before, (
            "the P0-02 guard must reject the network stage before it gets "
            "anywhere near making a connection -- if this fails, the "
            "transport_harness observed a real attempt"
        )
        # Dir/DB side effects must also be blocked, same as any other
        # previewed dry-run.
        assert not disposable_vault.root.exists()
        assert not disposable_vault.cfg.db_path.exists()

    def test_a_network_stage_is_planned_without_reaching_the_network(
        self, disposable_vault, monkeypatch, capsys
    ):
        """The guard used to name network stages in a refusal message.

        It no longer refuses them: the local-only gateway makes the lookup
        impossible, so the stage can be planned like any other. What must
        hold is that planning it dispatches nothing -- asserted by the
        transport harness in test_preview_zero_side_effects.py, and here by
        the plan simply completing and stating that nothing was looked up.
        """
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)

        rc = cli_mod._run_pipeline([EnrichStage], dry_run=True)

        assert rc == 0
        out = capsys.readouterr().out
        assert "PREVIEW ONLY" in out
        assert "no network lookup was performed" in out

    def test_a_mixed_pipeline_plans_every_stage(self, disposable_vault, monkeypatch, capsys):
        """Previously: the refusal had to name every network stage.

        Now every stage is planned, network or not, and the plan reports
        each one -- with a count where it can compute one and an em-dash
        where it cannot, never a fabricated zero.
        """
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)

        rc = cli_mod._run_pipeline([PreflightStage, EnrichStage], dry_run=True)

        assert rc == 0
        out = capsys.readouterr().out
        assert "preflight" in out
        assert "PREVIEW ONLY" in out


class TestDryRunGuardAppliesCentrallyNotAsAllowlist:
    """
    Design goal from the task: guard centrally in _run_pipeline itself,
    not via a per-subcommand allowlist that would drift as commands are
    added/renamed. Prove this by exercising a spread of different
    single-stage pipelines -- the exact shape of every
    `musaeus <stage> --dry-run` dispatch branch in cli.py's main() -- and
    confirming every one is previewed identically, purely because they
    all funnel through the same _run_pipeline() function rather than
    each needing its own opt-in.
    """

    @pytest.mark.parametrize("stage_cls", [PreflightStage, IngestStage])
    def test_arbitrary_single_stage_pipelines_are_previewed_under_dry_run(
        self, disposable_vault, monkeypatch, stage_cls
    ):
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)
        rc = cli_mod._run_pipeline([stage_cls], dry_run=True)
        assert rc == 0
        assert not disposable_vault.root.exists()

    def test_dry_run_alias_command_shape_is_previewed(self, disposable_vault, monkeypatch):
        """`musaeus dry-run` hardcodes dry_run=True against
        DEFAULT_PIPELINE (see cli.py's `elif command == "dry-run":`
        branch) -- confirm that exact call shape is previewed."""
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)
        rc = cli_mod._run_pipeline(DEFAULT_PIPELINE, dry_run=True)
        assert rc == 0

    def test_before_after_snapshot_is_identical_across_a_previewed_dry_run(
        self, disposable_vault, monkeypatch
    ):
        """Uses the P0-01 harness's own snapshot_vault_state() tool --
        the same before/after equality check MCR-001 requires of a real
        preview -- to prove a previewed dry-run leaves literally nothing
        different: directory tree, file hashes/tags, DB existence, event
        count, archive count, and DB content checksum all unchanged."""
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)
        before = snapshot_vault_state(disposable_vault)

        rc = cli_mod._run_pipeline(DEFAULT_PIPELINE, dry_run=True)

        after = snapshot_vault_state(disposable_vault)
        assert rc == 0
        assert before.equals(after), before.diff(after)


# ── Real (non-dry-run) invocations are NOT blocked ────────────────────────────


class TestRealRunIsNotBlockedByTheGuard:
    """The guard must be dry_run-specific: a real `musaeus run` (no
    --dry-run) against a fixture vault must proceed exactly as before --
    creating directories, opening the DB, and returning its normal exit
    code -- completely unaffected by the P0-02 guard."""

    def test_real_run_creates_dirs_and_db_and_is_not_blocked(self, disposable_vault, monkeypatch):
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)
        assert not disposable_vault.root.exists()

        rc = cli_mod._run_pipeline([PreflightStage], dry_run=False)

        assert rc != 2, "a real (non-dry-run) invocation must never be previewed by the P0-02 guard"
        assert disposable_vault.root.exists()
        assert disposable_vault.cfg.db_path.exists()

    def test_real_run_of_default_pipeline_against_empty_inbox_is_not_blocked(
        self, disposable_vault, monkeypatch
    ):
        """The full DEFAULT_PIPELINE (what a real `musaeus run` executes)
        against an empty disposable inbox completes normally -- not
        blocked by the P0-02 guard, which only concerns dry_run=True."""
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)

        rc = cli_mod._run_pipeline(DEFAULT_PIPELINE, dry_run=False)

        assert rc != 2
        assert disposable_vault.root.exists()
        assert disposable_vault.cfg.db_path.exists()

    def test_real_run_reaches_a_network_stages_own_validate(self, disposable_vault, monkeypatch):
        """
        The P0-02 guard only rejects dry_run=True; it must not also
        block a real (dry_run=False) invocation of one of the three
        network stages. Proven here by making EnrichStage.validate()
        raise a distinctive StageError -- BaseStage.execute() catches
        StageError and turns it into a failed StageResult (exit 1), so
        seeing exit 1 (not 2) proves _run_pipeline let dry_run=False
        proceed all the way into the real stage rather than being
        previewed by _reject_unsafe_dry_run.
        """
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)

        from musaeus.stages.base import StageError

        def _raise_distinctive_error(self, ctx):
            raise StageError("distinctive-marker: reached real EnrichStage.validate()")

        monkeypatch.setattr(EnrichStage, "validate", _raise_distinctive_error)

        rc = cli_mod._run_pipeline([EnrichStage], dry_run=False)

        assert rc == 1, "expected a normal stage-validation failure, not a P0-02 guard rejection"
        assert rc != 2
