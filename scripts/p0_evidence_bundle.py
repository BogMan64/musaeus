#!/usr/bin/env python3
"""
Collect the P0 acceptance evidence bundle (partial P0-19).

Runs each MCR-001..MCR-008 acceptance gate as a named selection of tests
and records what passed, so "the gate is met" is a link to executed test
output rather than an assertion in a document.

SCOPE, stated up front because P0-19 asks for more than this produces:
this bundle is MODULE-LEVEL evidence. P0-19 also requires representative
flows "through the real CLI and scheduler wrapper", and that is NOT done
-- the safety layer is built and proven, but the ~30 existing stages
still mutate the filesystem directly rather than through the P0-13
boundary, and cli.py has not been wired to the preflight/authority gate.
Until that integration lands, a CLI-level rehearsal would be rehearsing
the old path with new modules sitting beside it.

Run:  python3 scripts/p0_evidence_bundle.py [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# gate -> (requirement, test selection)
GATES: dict[str, tuple[str, str]] = {
    "no_external_network_in_default_preview": (
        "MCR-001",
        "tests/test_p0_14_duplicates_contract.py::TestNoProviderContact",
    ),
    "before_after_equality": (
        "MCR-001",
        "tests/test_p0_11_preflight_authority.py::TestPreflightPasses::"
        "test_preflight_creates_no_managed_state",
    ),
    "recovery_cap_and_usable_space_blocking": (
        "MCR-002",
        "tests/test_p0_12_checkpoint_recovery.py::TestCheckpoint",
    ),
    "authority_is_granted_never_inferred": (
        "MCR-002",
        "tests/test_p0_11_preflight_authority.py::TestAuthorityGate",
    ),
    "checkpoint_verified_before_mutation": (
        "MCR-003",
        "tests/test_p0_13_mutation_boundary.py::TestCapabilityGating",
    ),
    "rollback_after_partial_mutation": (
        "MCR-003",
        "tests/test_p0_13_mutation_boundary.py::TestRollbackRestoresExactly",
    ),
    "rollback_refuses_to_cause_loss": (
        "MCR-003",
        "tests/test_p0_13_mutation_boundary.py::TestRollbackRefusesUnexpectedOverwrite",
    ),
    "migration_backup_and_failure_recovery": (
        "MCR-004",
        "tests/test_p0_06_state_migration.py::TestFailedMigrationLeavesPriorStateUsable",
    ),
    "rebuild_parity": (
        "MCR-004",
        "tests/test_p0_07_canonical_events.py::TestRebuildParity",
    ),
    "exact_acoustid_columns_and_insertion": (
        "MCR-004",
        "tests/test_p0_14_duplicates_contract.py::TestAcoustIDStageCanActuallyRun "
        "tests/test_p0_14_duplicates_contract.py::TestDuplicateContract",
    ),
    "failed_stage_not_skipped_by_resume": (
        "MCR-005",
        "tests/test_p0_08_run_lifecycle.py::TestResumeEligibility",
    ),
    "prerequisite_blocking_names_recovery": (
        "MCR-005",
        "tests/test_p0_08_run_lifecycle.py::TestPrerequisiteGating",
    ),
    "cancellation_recorded_and_terminal_truthful": (
        "MCR-005",
        "tests/test_p0_09_cancellation.py",
    ),
    "lock_conflict_refuses_without_mutation": (
        "MCR-005",
        "tests/test_p0_10_scope_lock.py::TestMultiprocessConflict "
        "tests/test_p0_10_scope_lock.py::TestStaleLockHandling",
    ),
    "run_reports_distinguish_planned_from_applied": (
        "MCR-006",
        "tests/test_p0_16_reporting.py::TestPlannedVersusApplied",
    ),
    "shareable_redaction": (
        "MCR-006",
        "tests/test_p0_16_reporting.py::TestShareableRedaction",
    ),
    "curator_export_root_missing_blocks": (
        "MCR-007",
        "tests/test_p0_15_export_root.py::TestCuratorStageGuard",
    ),
    "scheduled_runs_are_review_only": (
        "MCR-008",
        "tests/test_p0_17_scheduled_runs.py",
    ),
}

# Required by P0-19 and NOT produced here. Listed explicitly so the bundle
# cannot be mistaken for a complete rehearsal.
NOT_COVERED: dict[str, str] = {
    "cli_level_rehearsal": (
        "P0-19 requires representative flows through the real CLI and the scheduler "
        "wrapper. The safety modules are proven in isolation; cli.py is not yet wired "
        "to the preflight/authority gate."
    ),
    "stages_routed_through_the_mutation_boundary": (
        "P0-13's other half. The ~30 existing stages still write to the filesystem "
        "directly, so a real run is not yet covered by checkpoint/rollback."
    ),
    "big_kahuna_missing_root_block": (
        "P0-19 lists this gate. --big-kahuna and BIG_KAHUNA_PIPELINE do not exist in "
        "this codebase; the equivalent live gate is Curator's export root, which is "
        "covered above. The spec needs updating."
    ),
    "documentation_consistency": ("P0-18 is not started."),
    "fsync_durability": (
        "The journal's fsync guards machine-level crash, which cannot be simulated "
        "here. Covered by design, not by a passing test -- see "
        "test_p0_12_checkpoint_recovery.py."
    ),
}


def run_gate(selection: str) -> dict[str, object]:
    args = [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"]
    args.extend(selection.split())
    proc = subprocess.run(args, capture_output=True, text=True)
    summary = [
        line for line in proc.stdout.strip().splitlines() if "passed" in line or "failed" in line
    ]
    return {
        "selection": selection,
        "passed": proc.returncode == 0,
        "summary": summary[-1].strip() if summary else proc.stdout[-200:],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="p0_evidence", help="output directory")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    results: dict[str, object] = {}
    for gate, (requirement, selection) in GATES.items():
        print(f"  running {gate} ...", flush=True)
        result = run_gate(selection)
        result["requirement"] = requirement
        results[gate] = result

    passed = sum(1 for r in results.values() if r["passed"])
    bundle = {
        "generated_at": generated,
        "scope": "fixture-only; module-level. NOT a CLI-level release rehearsal.",
        "live_data_operation": False,
        "gates_passed": passed,
        "gates_total": len(GATES),
        "gates": results,
        "not_covered": NOT_COVERED,
    }
    (out / "evidence.json").write_text(json.dumps(bundle, indent=2, sort_keys=True))

    lines = [
        "# MUSAEUS P0 acceptance evidence (partial)",
        "",
        f"Generated {generated}. **Fixture-only. No live data operation.**",
        "",
        f"Gates passing: **{passed}/{len(GATES)}**",
        "",
        "| Gate | Requirement | Result |",
        "|---|---|---|",
    ]
    for gate, result in results.items():
        mark = "pass" if result["passed"] else "FAIL"
        lines.append(f"| `{gate}` | {result['requirement']} | {mark} — {result['summary']} |")
    lines.extend(["", "## Required by P0-19 and not covered here", ""])
    for name, why in NOT_COVERED.items():
        lines.append(f"- **{name}** — {why}")
    (out / "evidence.md").write_text("\n".join(lines) + "\n")

    print(f"\n{passed}/{len(GATES)} gates passing -> {out}/evidence.md")
    return 0 if passed == len(GATES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
