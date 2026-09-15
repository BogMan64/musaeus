"""
P0-17 — scheduled invocation takes the same safety path as interactive.

The requirement with teeth: absent input and EOF must never be treated as
`y`. A run that mutates because nobody was there to say no is the worst
available failure, and it is the exact failure a cron slot invites.

No real schedule, library, or configuration is touched. The user crontab
entry is disabled and stays disabled; these tests never read or write it.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from musaeus.preflight import PreflightRequest, is_affirmative
from musaeus.safety.lock import Scope, acquire
from musaeus.scheduling import (
    EXIT_OK,
    EXIT_SAFETY_BLOCKED,
    MODE_REVIEW_ONLY,
    describe_outcome,
    run_scheduled,
    scheduled_response,
)
from musaeus.state.migrator import migrate
from tests.disposable_vault import snapshot_vault_state

DOMAIN = "library-mutation"
STARTED = "2026-08-24T03:00:00Z"


@pytest.fixture
def scheduled_request(disposable_vault, tmp_path) -> PreflightRequest:
    vault = disposable_vault
    source = vault.root / "INBOX"
    destination = vault.root / "ALAC-Library"
    lock_dir = tmp_path / "locks"
    for d in (vault.root, source, destination, lock_dir):
        d.mkdir(parents=True, exist_ok=True)
    conn = vault.open_db()
    conn.close()
    migrate(vault.cfg.db_path, recovery_root=vault.recovery_root)

    return PreflightRequest(
        scope=Scope.build(vault.root, DOMAIN),
        source_root=source,
        destination_root=destination,
        recovery_root=vault.recovery_root,
        db_path=vault.cfg.db_path,
        lock_dir=lock_dir,
        estimated_checkpoint_bytes=1024,
        estimated_items=4,
        configuration={"exports.curator.root": "/fixture/exports"},
        required_configuration_keys=("exports.curator.root",),
    )


def _replace(request: PreflightRequest, **over) -> PreflightRequest:
    from dataclasses import replace

    return replace(request, **over)


# ── Absent input is never yes ─────────────────────────────────────────────────


class TestAbsentInputIsNeverYes:
    def test_the_scheduled_response_is_none(self):
        """Not an empty string. A closed pipe yields "", and "" is one
        careless `.startswith()` away from being read as yes."""
        assert scheduled_response() is None

    @pytest.mark.parametrize("response", [None, "", " ", "\n", "\r\n"])
    def test_nothing_an_empty_pipe_can_produce_is_affirmative(self, response):
        assert is_affirmative(response) is False

    def test_eof_on_stdin_does_not_grant_authority(self, scheduled_request, monkeypatch):
        """Simulates the real cron condition: stdin present but at EOF."""
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        outcome = run_scheduled(scheduled_request, run_id="cron-1", started_at=STARTED)
        assert outcome.authority.granted is False
        assert outcome.mutated is False

    def test_a_clean_preflight_still_grants_nothing(self, scheduled_request):
        """The decisive case. Every check passes -- and authority is still
        not granted, because a schedule expresses 'do this regularly', not
        'you have my authority in advance'."""
        outcome = run_scheduled(scheduled_request, run_id="cron-1", started_at=STARTED)
        assert outcome.preflight.blocked is False
        assert outcome.authority.granted is False
        assert outcome.authority.reason_code == "authority_not_requested"
        assert outcome.exit_code == EXIT_OK
        assert outcome.mode == MODE_REVIEW_ONLY

    def test_allow_execution_true_still_cannot_grant_authority(self, scheduled_request):
        """The P0/P1 restriction is a value that can be asserted against,
        and forcing it does not open a path: authority additionally needs
        an affirmative response, which a scheduled run has none to give."""
        outcome = run_scheduled(
            scheduled_request, run_id="cron-1", started_at=STARTED, allow_execution=True
        )
        assert outcome.authority.granted is False
        assert outcome.mutated is False


# ── Conflicts defer, never force ──────────────────────────────────────────────


class TestLockConflict:
    def test_an_active_lock_defers_with_the_owner_named(self, scheduled_request):
        with acquire(scheduled_request.scope, scheduled_request.lock_dir, run_id="interactive"):
            outcome = run_scheduled(scheduled_request, run_id="cron-1", started_at=STARTED)

        assert outcome.exit_code == EXIT_SAFETY_BLOCKED
        assert outcome.reason_code == "lock_conflict"
        assert outcome.report.lock_observation["owner"]["run_id"] == "interactive"
        blocks = {b["name"] for b in outcome.report.safety_blocks}
        assert "lock_observation" in blocks

    def test_the_scheduled_run_never_steals_the_lock(self, scheduled_request):
        with acquire(
            scheduled_request.scope, scheduled_request.lock_dir, run_id="interactive"
        ) as held:
            run_scheduled(scheduled_request, run_id="cron-1", started_at=STARTED)
            from musaeus.safety.lock import observe

            still = observe(scheduled_request.scope, scheduled_request.lock_dir)
            assert still.run_id == "interactive"
            assert still.pid == held.owner.pid

    def test_a_free_scope_is_not_reported_as_a_conflict(self, scheduled_request):
        """Negative control."""
        outcome = run_scheduled(scheduled_request, run_id="cron-1", started_at=STARTED)
        assert outcome.reason_code != "lock_conflict"
        assert outcome.report.lock_observation["held"] is False


# ── Same blocks as interactive ────────────────────────────────────────────────


class TestIdenticalSafetyBlocks:
    def test_a_canonical_scope_is_refused_the_same_way(self, scheduled_request):
        req = _replace(
            scheduled_request,
            scope=Scope.build("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/ALAC-Library", DOMAIN),
        )
        outcome = run_scheduled(req, run_id="cron-1", started_at=STARTED)
        assert outcome.exit_code == EXIT_SAFETY_BLOCKED
        assert "scope_classification" in {b["name"] for b in outcome.report.safety_blocks}

    def test_invalid_configuration_blocks_and_reports_remediation(self, scheduled_request):
        req = _replace(scheduled_request, configuration={})
        outcome = run_scheduled(req, run_id="cron-1", started_at=STARTED)
        assert outcome.exit_code == EXIT_SAFETY_BLOCKED
        block = next(b for b in outcome.report.safety_blocks if b["name"] == "configuration_valid")
        assert "exports.curator.root" in block["remediation"]

    def test_the_scheduled_report_matches_the_interactive_preflight(self, scheduled_request):
        """Same path, so the same checks with the same outcomes -- not a
        parallel implementation that agrees today."""
        from musaeus.preflight import run_preflight

        interactive = run_preflight(scheduled_request)
        outcome = run_scheduled(scheduled_request, run_id="cron-1", started_at=STARTED)
        assert [c.name for c in interactive.checks] == [c.name for c in outcome.preflight.checks]
        assert [c.outcome for c in interactive.checks] == [
            c.outcome for c in outcome.preflight.checks
        ]


# ── Nothing is mutated, nothing real is touched ───────────────────────────────


class TestNoMutation:
    def test_a_scheduled_run_changes_nothing(self, disposable_vault, scheduled_request):
        before = snapshot_vault_state(disposable_vault)
        run_scheduled(scheduled_request, run_id="cron-1", started_at=STARTED)
        after = snapshot_vault_state(disposable_vault)

        sidecars = {
            f"{disposable_vault.cfg.db_path.name}-wal",
            f"{disposable_vault.cfg.db_path.name}-shm",
        }
        assert set(after.directory_tree) - set(before.directory_tree) <= sidecars
        assert before.db_content_checksum == after.db_content_checksum

    def test_the_scheduled_report_is_preview_mode_and_writes_no_file(
        self, scheduled_request, tmp_path
    ):
        from musaeus.reporting import MODE_PREVIEW, ReportError, write_report

        outcome = run_scheduled(scheduled_request, run_id="cron-1", started_at=STARTED)
        assert outcome.report.mode == MODE_PREVIEW
        report_root = tmp_path / "reports"
        with pytest.raises(ReportError):
            write_report(outcome.report, report_root)
        assert not report_root.exists()

    def test_no_real_schedule_is_read_or_written(self, scheduled_request, path_guard):
        """The user crontab entry is disabled and stays disabled. This
        module never reads it, and the PathGuard would raise if the test
        reached for anything under a protected real root."""
        import musaeus.scheduling as scheduling_mod

        source = Path(scheduling_mod.__file__).read_text()
        for forbidden in ("crontab", "systemd", "at -f", "/etc/cron"):
            assert forbidden not in source

        before = len(path_guard.attempts)
        run_scheduled(scheduled_request, run_id="cron-1", started_at=STARTED)
        assert len(path_guard.attempts) == before

    def test_the_log_record_states_what_happened(self, scheduled_request):
        record = describe_outcome(
            run_scheduled(scheduled_request, run_id="cron-1", started_at=STARTED)
        )
        assert record["mutated"] is False
        assert record["authority_granted"] is False
        assert record["mode"] == MODE_REVIEW_ONLY
        assert record["exit_code"] == EXIT_OK
