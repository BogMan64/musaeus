"""
P0-11 — read-only preflight and the execution-authority gate.

Ten blocking conditions, each proven to block, each proven to report
measured/required/remediation, and each with a negative control showing
the same check can pass. A check only ever observed saying one thing is
not a check.

The whole suite runs under the P0-01 PathGuard, which raises on any
access under PROTECTED_REAL_ROOTS -- including
/home/grey/Projects/MUSAEUS_RECOVERY. "Fixtures never create or probe the
future recovery root" is therefore enforced by the harness rather than
promised by a comment.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from musaeus.preflight import (
    AUTHORITY_PROMPT,
    BLOCK,
    PASS,
    RECOVERY_CAP_BYTES,
    PreflightError,
    PreflightRequest,
    estimate_recovery_requirement,
    evaluate_authority,
    is_affirmative,
    render_report,
    run_preflight,
)
from musaeus.safety.lock import Scope, acquire
from musaeus.state.migrator import migrate
from musaeus.state.policy import FUTURE_RECOVERY_ROOT
from tests.disposable_vault import snapshot_vault_state

DOMAIN = "library-mutation"


@pytest.fixture
def request_bits(disposable_vault, tmp_path):
    """A fully valid request. Every test below breaks exactly one thing."""
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
        estimated_quarantine_bytes=512,
        estimated_items=10,
        configuration={"exports.curator.root": "/fixture/exports"},
        required_configuration_keys=("exports.curator.root",),
        required_providers=(),
        consented_providers=frozenset(),
    )


def _replace(request: PreflightRequest, **over) -> PreflightRequest:
    from dataclasses import replace

    return replace(request, **over)


# ── The happy path, and that it changes nothing ───────────────────────────────


class TestPreflightPasses:
    def test_a_valid_request_passes_every_check(self, request_bits):
        report = run_preflight(request_bits)
        assert report.outcome == PASS, [c.name for c in report.blocking]
        assert report.blocked is False
        assert len(report.checks) == 10

    def test_preflight_creates_no_managed_state(self, disposable_vault, request_bits):
        """Read-only means read-only: no run, no event, no checkpoint, no
        directory, no lock."""
        before = snapshot_vault_state(disposable_vault)
        run_preflight(request_bits)
        after = snapshot_vault_state(disposable_vault)

        sidecars = {
            f"{disposable_vault.cfg.db_path.name}-wal",
            f"{disposable_vault.cfg.db_path.name}-shm",
        }
        new_paths = set(after.directory_tree) - set(before.directory_tree)
        assert new_paths <= sidecars, f"preflight created {sorted(new_paths - sidecars)}"
        assert before.db_content_checksum == after.db_content_checksum
        assert before.db_event_count == after.db_event_count

    def test_preflight_observes_the_lock_without_acquiring_it(self, request_bits):
        run_preflight(request_bits)
        assert not any(request_bits.lock_dir.glob("*.lock")), (
            "preflight must not take the lock it is asking about"
        )
        # And the scope is still acquirable afterwards.
        with acquire(request_bits.scope, request_bits.lock_dir, run_id="run-A") as handle:
            assert handle.owner.run_id == "run-A"

    def test_report_carries_the_fixed_policy_without_touching_it(self, request_bits, path_guard):
        before = len(path_guard.attempts)
        report = run_preflight(request_bits)
        assert report.recovery_policy["future_recovery_root"] == FUTURE_RECOVERY_ROOT
        assert report.recovery_policy["recovery_cap_bytes"] == RECOVERY_CAP_BYTES
        assert report.recovery_policy["probed"] is False
        assert len(path_guard.attempts) == before

    def test_report_is_a_valid_preflight_event_payload(self, request_bits):
        from musaeus.state.events import RUN_PREFLIGHT_COMPLETED, new_event

        report = run_preflight(request_bits)
        event = new_event("run-A", 1, RUN_PREFLIGHT_COMPLETED, report.as_event_payload())
        assert event.payload["outcome"] == PASS
        assert isinstance(event.payload["checks"], list)
        assert all(isinstance(c, dict) for c in event.payload["checks"])

    def test_render_report_names_the_cap_and_the_future_root(self, request_bits):
        text = render_report(run_preflight(request_bits))
        assert "100 GB" in text
        assert FUTURE_RECOVERY_ROOT in text
        assert "not created or probed" in text


# ── The ten blocking conditions ───────────────────────────────────────────────


class TestBlockingConditions:
    def test_canonical_scope_is_blocked(self, request_bits):
        req = _replace(
            request_bits,
            scope=Scope.build("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/ALAC-Library", DOMAIN),
        )
        report = run_preflight(req)
        check = report.check("scope_classification")
        assert check.outcome == BLOCK
        assert check.reason_code == "authority_denied"
        assert check.measured == "canonical"
        assert "read-only" in check.remediation

    def test_inbox_scope_is_reported_not_treated_as_approval(self, request_bits):
        """MCR-002: 'the locked INBOX/canonical boundary must be reported,
        not treated as live approval'."""
        req = _replace(
            request_bits, scope=Scope.build("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/INBOX", DOMAIN)
        )
        report = run_preflight(req)
        assert report.check("scope_classification").measured == "inbox"
        assert report.blocked is True
        assert evaluate_authority(report, "y").granted is False

    def test_unreadable_source_is_blocked(self, request_bits):
        req = _replace(request_bits, source_root=request_bits.source_root / "missing")
        check = run_preflight(req).check("source_readable")
        assert check.outcome == BLOCK
        assert check.measured == "absent"
        assert "source root" in check.remediation

    def test_unwritable_destination_is_blocked(self, request_bits):
        dest = request_bits.destination_root
        original = stat.S_IMODE(dest.stat().st_mode)
        dest.chmod(0o555)
        try:
            if os.access(dest, os.W_OK):
                pytest.skip("running with write override (root)")
            check = run_preflight(request_bits).check("destination_writable")
            assert check.outcome == BLOCK
            assert check.measured == "unwritable"
        finally:
            dest.chmod(original)

    def test_missing_recovery_target_is_blocked(self, request_bits, tmp_path):
        req = _replace(request_bits, recovery_root=tmp_path / "no-such-recovery")
        report = run_preflight(req)
        assert report.check("recovery_target").outcome == BLOCK
        assert report.check("recovery_capacity").outcome == BLOCK
        assert report.check("recovery_capacity").measured is None

    def test_the_future_recovery_root_is_refused_without_being_probed(
        self, request_bits, path_guard
    ):
        before = len(path_guard.attempts)
        req = _replace(request_bits, recovery_root=Path(FUTURE_RECOVERY_ROOT))
        check = run_preflight(req).check("recovery_target")
        assert check.outcome == BLOCK
        assert check.reason_code == "recovery_target_invalid"
        assert len(path_guard.attempts) == before

    def test_requirement_above_the_fixed_cap_is_blocked(self, request_bits):
        req = _replace(request_bits, estimated_checkpoint_bytes=RECOVERY_CAP_BYTES + 1)
        check = run_preflight(req).check("recovery_cap")
        assert check.outcome == BLOCK
        assert check.reason_code == "recovery_capacity_exceeded"
        assert check.required == RECOVERY_CAP_BYTES
        assert check.measured > RECOVERY_CAP_BYTES
        assert "100 GB" in check.remediation

    def test_the_cap_boundary_is_exact(self, request_bits):
        """Pins the boundary on both sides rather than asserting the
        outcome is one of the two possible outcomes -- which is what the
        first version of this test did, and which cannot fail.

        estimate_recovery_requirement() is checkpoint + quarantine + the
        database's own size + manifest overhead, so the checkpoint figure
        is solved backwards from the cap."""
        db_bytes = request_bits.db_path.stat().st_size
        at_cap = _replace(
            request_bits,
            estimated_checkpoint_bytes=RECOVERY_CAP_BYTES - db_bytes,
            estimated_quarantine_bytes=0,
            estimated_items=0,
        )
        assert estimate_recovery_requirement(at_cap) == RECOVERY_CAP_BYTES
        assert run_preflight(at_cap).check("recovery_cap").outcome == PASS

        over_cap = _replace(at_cap, estimated_checkpoint_bytes=RECOVERY_CAP_BYTES - db_bytes + 1)
        assert estimate_recovery_requirement(over_cap) == RECOVERY_CAP_BYTES + 1
        assert run_preflight(over_cap).check("recovery_cap").outcome == BLOCK

    def test_capacity_accounting_includes_manifest_and_database_overhead(self, request_bits):
        """MCR-002 requires the estimate to cover checkpoint, quarantine,
        database and manifest material -- not just the file bytes."""
        from musaeus.preflight import MANIFEST_BYTES_PER_ITEM

        db_bytes = request_bits.db_path.stat().st_size
        assert db_bytes > 0
        required = estimate_recovery_requirement(request_bits)
        assert required == (
            request_bits.estimated_checkpoint_bytes
            + request_bits.estimated_quarantine_bytes
            + db_bytes
            + request_bits.estimated_items * MANIFEST_BYTES_PER_ITEM
        )
        assert required > (
            request_bits.estimated_checkpoint_bytes + request_bits.estimated_quarantine_bytes
        )

    def test_insufficient_safely_usable_space_is_blocked(self, request_bits):
        req = _replace(request_bits, safety_reserve_fraction=1.0)
        check = run_preflight(req).check("recovery_capacity")
        assert check.outcome == BLOCK
        assert check.measured == 0
        assert check.required > 0
        assert "safely usable" in check.remediation

    def test_incompatible_schema_is_blocked(self, request_bits):
        conn = sqlite3.connect(str(request_bits.db_path), isolation_level=None)
        try:
            conn.execute("UPDATE state_metadata SET schema_version = 999 WHERE id = 1")
        finally:
            conn.close()
        check = run_preflight(request_bits).check("schema_compatible")
        assert check.outcome == BLOCK
        assert check.reason_code == "schema_incompatible"
        assert check.measured == 999

    def test_an_unfinished_migration_is_blocked(self, request_bits):
        conn = sqlite3.connect(str(request_bits.db_path), isolation_level=None)
        try:
            conn.execute("UPDATE schema_migrations SET outcome='running', finished_at=NULL")
        finally:
            conn.close()
        check = run_preflight(request_bits).check("schema_compatible")
        assert check.outcome == BLOCK
        assert check.reason_code == "migration_incomplete"

    def test_invalid_configuration_is_blocked(self, request_bits):
        req = _replace(request_bits, configuration={})
        check = run_preflight(req).check("configuration_valid")
        assert check.outcome == BLOCK
        assert check.reason_code == "configuration_invalid"
        assert "exports.curator.root" in check.remediation

    def test_empty_configuration_value_counts_as_missing(self, request_bits):
        req = _replace(request_bits, configuration={"exports.curator.root": ""})
        assert run_preflight(req).check("configuration_valid").outcome == BLOCK

    def test_missing_provider_consent_is_blocked(self, request_bits):
        req = _replace(request_bits, required_providers=("lastfm", "musicbrainz"))
        check = run_preflight(req).check("provider_consent")
        assert check.outcome == BLOCK
        assert check.reason_code == "provider_not_enabled"
        assert "lastfm" in check.remediation

    def test_granted_provider_consent_passes(self, request_bits):
        req = _replace(
            request_bits,
            required_providers=("lastfm",),
            consented_providers=frozenset({"lastfm"}),
        )
        assert run_preflight(req).check("provider_consent").outcome == PASS

    def test_an_active_lock_is_blocked_and_names_the_owner(self, request_bits):
        with acquire(request_bits.scope, request_bits.lock_dir, run_id="other-run"):
            report = run_preflight(request_bits)
        check = report.check("lock_observation")
        assert check.outcome == BLOCK
        assert check.reason_code == "lock_conflict"
        assert "other-run" in check.remediation
        assert report.lock_observation["held"] is True
        assert report.lock_observation["owner"]["run_id"] == "other-run"

    def test_every_check_is_evaluated_even_after_one_blocks(self, request_bits):
        """No short-circuit. Fixing one problem at a time, each discovered
        only after another full attempt, is how a five-minute fix becomes
        an evening."""
        req = _replace(
            request_bits,
            source_root=request_bits.source_root / "missing",
            configuration={},
            required_providers=("lastfm",),
        )
        report = run_preflight(req)
        blocked = {c.name for c in report.blocking}
        assert {"source_readable", "configuration_valid", "provider_consent"} <= blocked
        assert len(report.checks) == 10

    def test_every_blocking_check_reports_remediation(self, request_bits):
        req = _replace(
            request_bits,
            source_root=request_bits.source_root / "missing",
            configuration={},
            required_providers=("lastfm",),
            estimated_checkpoint_bytes=RECOVERY_CAP_BYTES + 1,
        )
        for check in run_preflight(req).blocking:
            assert check.remediation, f"{check.name} blocks without saying what to do"
            assert check.reason_code, f"{check.name} blocks without a reason code"


# ── The authority transition ──────────────────────────────────────────────────


class TestAuthorityGate:
    def test_the_prompt_is_exactly_as_specified(self):
        assert AUTHORITY_PROMPT == "Proceed with authorised execution? [y/N] "

    @pytest.mark.parametrize("response", ["y", " y ", "y\n"])
    def test_explicit_y_grants_authority_after_a_clean_preflight(self, request_bits, response):
        decision = evaluate_authority(run_preflight(request_bits), response)
        assert decision.granted is True
        assert decision.reason_code == "authority_granted"
        decision.require()

    @pytest.mark.parametrize(
        "response", [None, "", " ", "\n", "n", "N", "no", "Y", "yes", "YES", "sure", "1"]
    )
    def test_everything_that_is_not_lowercase_y_is_no(self, request_bits, response):
        """`None` is what absent input and EOF look like to a scheduled
        run (P0-17). A non-interactive invocation must never fall through
        to yes because nobody was there to say no."""
        assert is_affirmative(response) is False
        decision = evaluate_authority(run_preflight(request_bits), response)
        assert decision.granted is False
        assert decision.reason_code == "authority_not_requested"
        with pytest.raises(PreflightError):
            decision.require()

    def test_y_does_not_override_a_blocked_preflight(self, request_bits):
        req = _replace(request_bits, configuration={})
        decision = evaluate_authority(run_preflight(req), "y")
        assert decision.granted is False
        assert decision.reason_code == "preflight_blocked"

    def test_a_blocked_preflight_reports_blocked_rather_than_unconfirmed(self, request_bits):
        """When both are true, 'you cannot' is more useful than 'you did
        not confirm'."""
        req = _replace(request_bits, configuration={})
        assert evaluate_authority(run_preflight(req), None).reason_code == "preflight_blocked"

    def test_authority_is_not_inferred_from_a_passing_preflight(self, request_bits):
        """Every check passing makes authority available. It does not grant
        it."""
        report = run_preflight(request_bits)
        assert report.blocked is False
        assert evaluate_authority(report, None).granted is False
