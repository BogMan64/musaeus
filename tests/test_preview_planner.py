"""P0-04 unit and disposable-fixture regressions for typed preview planning."""

from __future__ import annotations

import io
import json
import sqlite3
import sys

import pytest

from musaeus import cli
from musaeus.context import RunContext
from musaeus.planning import (
    INVALID_USAGE_EXIT_CODE,
    PREVIEW_BLOCKED_EXIT_CODE,
    PREVIEW_COMPLETE_EXIT_CODE,
    CommandRequest,
    PreviewOutputFormat,
    PreviewUsageError,
    RunMode,
    build_preview_plan,
    render_preview,
)
from musaeus.preview_guard import LEGACY_PREVIEW_EXIT_CODE, LEGACY_PREVIEW_MESSAGE


def test_parser_resolves_preview_spellings_to_the_typed_preview_mode() -> None:
    """**Validates: Requirements MCR-001, MCR-006, MCR-007**"""
    parser = cli._build_parser()

    execute_args = parser.parse_args(["run"])
    dry_run_args = parser.parse_args(["run", "--dry-run"])
    preview_args = parser.parse_args(["preview"])

    assert execute_args.run_mode is RunMode.EXECUTE
    assert dry_run_args.run_mode is RunMode.PREVIEW
    assert preview_args.run_mode is RunMode.PREVIEW
    assert cli._command_request_from_args(dry_run_args).mode is RunMode.PREVIEW
    assert cli._command_request_from_args(preview_args).mode is RunMode.PREVIEW


@pytest.mark.parametrize(
    ("argv", "option"),
    (
        (("run", "--dry-run", "--reset"), "--reset"),
        (("curator", "--dry-run", "--export-root", "fixture-export"), "--export-root"),
    ),
)
def test_preview_rejects_ambiguous_persistence_options(argv: tuple[str, ...], option: str) -> None:
    """**Validates: Requirements MCR-001, MCR-006**"""
    with pytest.raises(PreviewUsageError, match=option) as raised:
        cli._command_request_from_args(cli._build_parser().parse_args(argv))

    assert raised.value.exit_code == INVALID_USAGE_EXIT_CODE


def test_preview_plan_is_deterministic_and_never_derives_authority() -> None:
    """**Validates: Requirements MCR-001, MCR-006, MCR-007**"""
    request = CommandRequest(
        command="run",
        mode=RunMode.PREVIEW,
        stage_names=("IngestStage", "SentinelStage"),
        declared_scope="/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/INBOX",
        assumptions=("container context was supplied", "flag requested execution"),
    )

    first = build_preview_plan(request)
    second = build_preview_plan(request)

    assert first == second
    assert first.plan.mode is RunMode.PREVIEW
    assert first.plan.execution_authority == "not_requested_or_granted"
    assert all(not action.requires_execution_authority for action in first.plan.actions)
    assert first.managed_state_changed is False
    assert first.external_lookup_performed is False


def test_preview_planner_does_not_instantiate_mutation_capable_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Planner propagation uses a stage descriptor only, never a stage instance."""

    class MutationCapableStage:
        NAME = "mutation-capable"

        def __init__(self) -> None:
            raise AssertionError("planning must not instantiate mutation-capable stages")

    monkeypatch.setattr(cli, "IngestStage", MutationCapableStage)
    request = cli._command_request_from_args(
        cli._build_parser().parse_args(["ingest", "--dry-run"])
    )

    result = build_preview_plan(request)

    assert result.plan.actions[0].stage_id == "MutationCapableStage"
    assert result.plan.actions[0].operation == "stage_evaluation"


def test_preview_renderers_use_only_in_memory_or_stdout_streams() -> None:
    """**Validates: Requirements MCR-001, MCR-006**"""
    result = build_preview_plan(
        CommandRequest(
            command="preview",
            mode=RunMode.PREVIEW,
            stage_names=("IngestStage",),
            declared_scope="fixture-scope",
        )
    )
    human = io.StringIO()
    machine = io.StringIO()

    render_preview(result, PreviewOutputFormat.HUMAN, human)
    render_preview(result, PreviewOutputFormat.JSON, machine)

    assert "Managed state: unchanged" in human.getvalue()
    assert "External lookup: not performed" in human.getvalue()
    rendered = json.loads(machine.getvalue())
    assert rendered["plan"]["proposed_action_count"] == 1
    assert rendered["managed_state_changed"] is False
    assert rendered["external_lookup_performed"] is False


def test_preview_context_cannot_be_created_or_append_an_event(
    disposable_vault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preview remains a Plan/PreviewResult, not a mutation-capable RunContext."""
    from musaeus import context as context_module

    attempts: list[str] = []
    connection = sqlite3.connect(":memory:")
    monkeypatch.setattr(
        context_module,
        "log_event",
        lambda *_args, **_kwargs: attempts.append("event"),
    )
    try:
        with pytest.raises(ValueError, match="execution-only"):
            RunContext.new(disposable_vault.music_config(), connection, mode=RunMode.PREVIEW)
    finally:
        connection.close()

    assert attempts == []


def test_public_preview_uses_the_pure_planner_after_p0_05(
    disposable_vault,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The typed public route replaces P0-02 only after fixture proof is present."""
    cli_module = disposable_vault.prepare_legacy_cli(monkeypatch)
    boundary_calls: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "get_config",
        lambda: boundary_calls.append("configuration"),
    )
    monkeypatch.setattr(sys, "argv", ["musaeus", "preview"])

    with pytest.raises(SystemExit) as exited:
        cli_module.main()

    captured = capsys.readouterr()
    assert exited.value.code == PREVIEW_COMPLETE_EXIT_CODE
    assert (
        "Safety: No managed state was changed; external lookup was not performed." in captured.out
    )
    assert LEGACY_PREVIEW_MESSAGE not in captured.out
    assert captured.err == ""
    assert boundary_calls == []
    # The compatibility guard's non-success code remains reserved for direct,
    # unsafe entry points which are still deliberately blocked.
    assert LEGACY_PREVIEW_EXIT_CODE == PREVIEW_BLOCKED_EXIT_CODE
