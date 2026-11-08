"""
P0-16 — run-scoped reports and shareable redaction.

The property under test throughout is that the report distinguishes
*planned* from *applied*. DR-08 asks for it and it is the difference
between "we would move 11,160 files" and "we moved 11,160 files" -- a
report that blurs the two is worse than none, because it reads as
authoritative.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from musaeus.reporting import (
    MODE_EXECUTE,
    MODE_PREVIEW,
    REDACTED,
    ActionCounts,
    ReportError,
    RunReport,
    StageReport,
    item_ref_for_path,
    render_human,
    render_json,
    to_shareable,
    write_report,
)


def _report(**over) -> RunReport:
    base: dict = {
        "run_id": "run-A",
        "mode": MODE_EXECUTE,
        "scope_root": "/fixtures/vault",
        "classification": "fixture",
        "started_at": "2026-08-24T05:00:00Z",
        "finished_at": "2026-08-24T05:04:00Z",
        "status": "succeeded",
        "exit_code": 0,
        "config_digest": "cfg-abc",
        "stages": (
            StageReport(
                stage_id="IngestStage",
                attempt=1,
                status="succeeded",
                counts=ActionCounts(planned=10, applied=10),
            ),
            StageReport(
                stage_id="ForgeStage",
                attempt=1,
                status="failed",
                counts=ActionCounts(planned=10, applied=4, failed=6, rolled_back=4),
                error_code="stage_failed",
                blockers=("/fixtures/vault/Bob Seger/Night Moves.m4a",),
                recovery_action="retry ForgeStage after freeing space in /fixtures/recovery",
            ),
        ),
        "totals": ActionCounts(planned=20, applied=14, failed=6, rolled_back=4),
        "checkpoint_id": "ckpt-1",
        "manifest_digest": "d" * 64,
        "quarantine_refs": ("q_1", "q_2"),
        "rollback_status": "completed",
        "lock_observation": {
            "held": True,
            "owner": {"run_id": "other", "pid": 4242, "scope_root": "/fixtures/vault"},
        },
        "authority": "granted",
        "recovery_target": "/fixtures/recovery",
        "network_use": (
            {"provider": "acoustid", "outcome": "denied", "api_key": "super-secret-value"},
        ),
        "next_actions": ("inspect /fixtures/recovery/ckpt-1 before retrying",),
        "path_map": {item_ref_for_path("/fixtures/vault/a.m4a"): "/fixtures/vault/a.m4a"},
    }
    base.update(over)
    return RunReport(**base)


# ── Planned vs applied ────────────────────────────────────────────────────────


class TestPlannedVersusApplied:
    def test_they_are_separate_fields_not_one_number(self):
        counts = ActionCounts(planned=20, applied=14)
        assert counts.as_dict()["planned"] == 20
        assert counts.as_dict()["applied"] == 14

    def test_json_reports_every_required_count(self):
        data = json.loads(render_json(_report()))
        for name in ("planned", "applied", "skipped", "failed", "cancelled", "rolled_back"):
            assert name in data["totals"], f"DR-08 requires a {name} count"

    def test_human_output_shows_both_on_the_same_line(self):
        text = render_human(_report())
        header = next(line for line in text.splitlines() if "planned" in line)
        assert "applied" in header, "planned and applied must not be separated"
        total_line = next(line for line in text.splitlines() if line.strip().startswith("total"))
        assert "20" in total_line and "14" in total_line

    def test_per_stage_counts_are_reported(self):
        text = render_human(_report())
        assert "ForgeStage" in text
        assert "retry ForgeStage" in text


# ── Report contents ───────────────────────────────────────────────────────────


class TestReportContents:
    def test_the_report_carries_everything_dr08_requires(self):
        data = json.loads(render_json(_report()))
        assert data["run_id"] == "run-A"
        assert data["scope"]["classification"] == "fixture"
        assert data["timestamps"]["started_at"]
        assert data["config_digest"] == "cfg-abc"
        assert data["recovery"]["checkpoint_id"] == "ckpt-1"
        assert data["recovery"]["quarantine_refs"] == ["q_1", "q_2"]
        assert data["recovery"]["rollback_status"] == "completed"
        assert data["recovery"]["recovery_cap"] == "100 GB"
        assert data["lock_observation"]["held"] is True
        assert data["authority"] == "granted"
        assert data["next_actions"]

    def test_safety_blocks_carry_reason_and_remediation(self):
        report = _report(
            safety_blocks=(
                {
                    "name": "recovery_capacity",
                    "reason_code": "recovery_capacity_exceeded",
                    "remediation": "free space on the recovery target",
                },
            )
        )
        text = render_human(report)
        assert "recovery_capacity_exceeded" in text
        assert "free space on the recovery target" in text

    def test_human_output_names_the_lock_owner(self):
        text = render_human(_report())
        assert "run other" in text
        assert "4242" in text


# ── Shareable redaction ───────────────────────────────────────────────────────


class TestShareableRedaction:
    def test_paths_become_stable_item_references(self):
        shareable = to_shareable(_report())
        assert shareable.scope_root == item_ref_for_path("/fixtures/vault")
        assert shareable.scope_root.startswith("item:")
        assert "/fixtures/vault" not in render_json(shareable)

    def test_item_correlation_survives_redaction(self):
        """Redaction must not destroy the ability to say 'this is the same
        item as that one' -- a shareable report where every reference is
        unique noise cannot be discussed."""
        a = to_shareable(_report())
        b = to_shareable(_report())
        assert a.scope_root == b.scope_root
        assert a.recovery_target == b.recovery_target

    def test_credential_shaped_values_are_redacted(self):
        text = render_json(to_shareable(_report()))
        assert "super-secret-value" not in text
        assert REDACTED in text

    def test_the_restricted_path_map_is_dropped_not_redacted(self):
        """A map from references back to paths is exactly the thing that
        would undo the redaction."""
        shareable = to_shareable(_report())
        assert shareable.path_map == {}
        assert "restricted_path_map" not in shareable.as_dict()

    def test_paths_inside_nested_structures_are_redacted(self):
        shareable = to_shareable(_report())
        text = render_json(shareable)
        assert "/fixtures/vault/Bob Seger/Night Moves.m4a" not in text
        assert "/fixtures/recovery" not in text

    def test_the_restricted_report_keeps_paths_because_recovery_needs_them(self):
        """Negative control. 'Restore the quarantined item' is not
        actionable without knowing where it came from."""
        data = json.loads(render_json(_report()))
        assert data["scope"]["root"] == "/fixtures/vault"
        assert data["restricted_path_map"]

    def test_an_unknown_path_embedded_in_prose_is_redacted(self):
        """Isolates the regex pass. The report has never seen this path,
        so the known-path exact pass cannot cover it -- only the pattern
        can. Without this, disabling the regex still passed, because every
        path in the fixture happened to be a known one."""
        report = _report(
            next_actions=("check /var/spool/musaeus/pending before retrying",),
            path_map={},
            recovery_target=None,
        )
        shareable = to_shareable(report)
        assert "/var/spool/musaeus/pending" not in shareable.next_actions[0]
        assert "item:" in shareable.next_actions[0]
        assert "before retrying" in shareable.next_actions[0], "the sentence must survive"

    def test_a_known_path_containing_spaces_is_redacted_inside_prose(self):
        """Isolates the exact-match pass. A path with spaces cannot be
        delimited from surrounding prose by any pattern -- and library
        paths are full of spaces. Only knowing the path in advance works.
        Without this, disabling that pass still passed."""
        noisy = "/fixtures/vault/Bob Seger/Night Moves.m4a"
        report = _report(
            path_map={item_ref_for_path(noisy): noisy},
            next_actions=(f"re-tag {noisy} by hand",),
        )
        shareable = to_shareable(report)
        assert noisy not in shareable.next_actions[0]
        assert item_ref_for_path(noisy) in shareable.next_actions[0]

    def test_prose_without_a_path_passes_through_the_redactor_unchanged(self):
        """Isolates over-eager redaction on a field that actually goes
        through the redactor. The earlier version asserted on stage_id and
        status, which never pass through it, so a redactor that mangled
        everything still passed."""
        report = _report(next_actions=("retry the run once space is available",))
        shareable = to_shareable(report)
        assert shareable.next_actions[0] == "retry the run once space is available"

    def test_ordinary_text_is_not_mangled_by_the_redactor(self):
        """A redactor that guesses aggressively turns reason codes and
        stage names into opaque references and makes the shareable report
        unreadable -- its own way of being useless."""
        shareable = to_shareable(_report())
        assert shareable.stages[1].stage_id == "ForgeStage"
        assert shareable.stages[1].error_code == "stage_failed"
        assert shareable.classification == "fixture"
        assert shareable.status == "succeeded"


# ── Persistence ───────────────────────────────────────────────────────────────


class TestPersistence:
    def test_preview_writes_no_report_file(self, tmp_path):
        report_root = tmp_path / "reports"
        with pytest.raises(ReportError) as exc:
            write_report(_report(mode=MODE_PREVIEW), report_root)
        assert "preview does not write reports" in str(exc.value)
        assert not report_root.exists(), "not even the directory may be created"

    def test_execution_writes_both_forms(self, tmp_path):
        report_root = tmp_path / "reports"
        restricted, shareable = write_report(_report(), report_root)

        assert restricted.is_file() and shareable.is_file()
        restricted_text = restricted.read_text()
        shareable_text = shareable.read_text()

        assert "/fixtures/vault" in restricted_text
        assert "/fixtures/vault" not in shareable_text
        assert "super-secret-value" not in restricted_text, (
            "a credential in the restricted report is still a credential on disk"
        )
        assert "super-secret-value" not in shareable_text

    def test_both_forms_agree_about_what_happened(self):
        """Rendered from one RunReport, so they cannot drift into
        disagreeing -- the difference is a projection, not a second
        write-up."""
        report = _report()
        restricted = json.loads(render_json(report))
        shareable = json.loads(render_json(to_shareable(report)))
        assert restricted["totals"] == shareable["totals"]
        assert restricted["status"] == shareable["status"]
        assert [s["stage_id"] for s in restricted["stages"]] == [
            s["stage_id"] for s in shareable["stages"]
        ]

    def test_no_mail_capability_exists(self):
        """A future P2 Thunderbird adapter consumes the redacted payload.
        It is not here, and nothing in this module sends anything.

        Asserted against imports and the public API, not against raw
        substrings: the first version searched the source text for "SMTP"
        and failed on the module docstring's own sentence saying no SMTP
        credential is read. A test that cannot distinguish a capability
        from a promise not to have one is not testing the capability."""
        import ast

        import musaeus.reporting as reporting_mod

        tree = ast.parse(Path(reporting_mod.__file__).read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not ({"smtplib", "email", "socket", "requests", "urllib"} & imported), (
            f"a transport library is imported: {sorted(imported)}"
        )

        public = [n for n in dir(reporting_mod) if not n.startswith("_")]
        assert [n for n in public if any(w in n.lower() for w in ("send", "mail", "smtp"))] == []
