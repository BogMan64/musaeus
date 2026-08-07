"""Fixture-only P0-03 coverage for the Big Kahuna compatibility block."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from musaeus import cli
from musaeus import setup as setup_api
from musaeus.preview_guard import LEGACY_PREVIEW_EXIT_CODE, LEGACY_PREVIEW_MESSAGE


def _unexpected_boundary_call(name: str, calls: list[str]):
    def unexpected(*_args: object, **_kwargs: object) -> object:
        calls.append(name)
        raise AssertionError(f"{name} must not run for a blocked Big Kahuna invocation")

    return unexpected


@pytest.mark.parametrize(
    "argv",
    (
        ("run", "--big-kahuna"),
        ("run", "--big-kahuna", "--reset"),
    ),
    ids=("big-kahuna", "big-kahuna-reset"),
)
def test_big_kahuna_missing_root_blocks_before_all_managed_boundaries(
    argv: tuple[str, ...],
    disposable_vault,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI P0-03 exits before setup, resume reset, config, DB, or stage work."""
    disposable_vault.initialise_database()
    disposable_vault.snapshot()
    before = disposable_vault.snapshot()
    cli_module = disposable_vault.prepare_legacy_cli(monkeypatch)
    boundary_calls: list[str] = []

    for target, name in (
        ("_setup_logging", "logging setup"),
        ("_clear_resume", "resume reset"),
        ("get_config", "configuration resolution"),
        ("open_db", "database initialisation"),
        ("_run_pipeline", "pipeline dispatch"),
    ):
        monkeypatch.setattr(
            cli_module,
            target,
            _unexpected_boundary_call(name, boundary_calls),
        )
    monkeypatch.setattr(
        cli_module.RunContext,
        "new",
        _unexpected_boundary_call("run context construction", boundary_calls),
    )
    for stage_class in cli_module.BIG_KAHUNA_PIPELINE:
        monkeypatch.setattr(
            stage_class,
            "__init__",
            _unexpected_boundary_call(f"{stage_class.__name__} construction", boundary_calls),
        )
    monkeypatch.setattr(
        setup_api,
        "needs_setup",
        _unexpected_boundary_call("first-run setup check", boundary_calls),
    )
    monkeypatch.setattr(
        setup_api,
        "run_wizard",
        _unexpected_boundary_call("setup wizard", boundary_calls),
    )
    monkeypatch.setattr(sys, "argv", ["musaeus", *argv])

    with pytest.raises(SystemExit) as exited:
        cli_module.main()

    assert exited.value.code == cli_module.BIG_KAHUNA_EXPORT_ROOT_EXIT_CODE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{cli_module.BIG_KAHUNA_EXPORT_ROOT_MESSAGE}\n"
    assert boundary_calls == []
    after = disposable_vault.snapshot()
    assert after == before, after.difference_from(before)
    assert disposable_vault.transport.attempts == []
    assert disposable_vault.subprocesses.attempts == []
    assert disposable_vault.path_guard.write_attempts == []


@pytest.mark.parametrize(
    "copy_pipeline",
    (False, True),
    ids=("canonical-list", "copied-canonical-list"),
)
def test_direct_big_kahuna_pipeline_blocks_before_configuration_database_or_stages(
    copy_pipeline: bool,
    disposable_vault,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Canonical and copied Big Kahuna lists block before managed work begins."""
    disposable_vault.initialise_database()
    disposable_vault.snapshot()
    before = disposable_vault.snapshot()
    cli_module = disposable_vault.prepare_legacy_cli(monkeypatch)
    boundary_calls: list[str] = []
    stages = (
        list(cli_module.BIG_KAHUNA_PIPELINE) if copy_pipeline else cli_module.BIG_KAHUNA_PIPELINE
    )
    if copy_pipeline:
        assert stages is not cli_module.BIG_KAHUNA_PIPELINE

    for target, name in (
        ("get_config", "configuration resolution"),
        ("open_db", "database initialisation"),
        ("_load_resume", "resume lookup"),
    ):
        monkeypatch.setattr(
            cli_module,
            target,
            _unexpected_boundary_call(name, boundary_calls),
        )
    monkeypatch.setattr(
        cli_module.RunContext,
        "new",
        _unexpected_boundary_call("run context construction", boundary_calls),
    )
    for stage_class in cli_module.BIG_KAHUNA_PIPELINE:
        monkeypatch.setattr(
            stage_class,
            "__init__",
            _unexpected_boundary_call(f"{stage_class.__name__} construction", boundary_calls),
        )

    assert (
        cli_module._run_pipeline(stages, dry_run=False)
        == cli_module.BIG_KAHUNA_EXPORT_ROOT_EXIT_CODE
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{cli_module.BIG_KAHUNA_EXPORT_ROOT_MESSAGE}\n"
    assert boundary_calls == []
    after = disposable_vault.snapshot()
    assert after == before, after.difference_from(before)
    assert disposable_vault.transport.attempts == []
    assert disposable_vault.subprocesses.attempts == []
    assert disposable_vault.path_guard.write_attempts == []


@pytest.mark.parametrize(
    ("argv", "expected_pipeline"),
    (
        (("run", "--maintain", "--big-kahuna"), cli.MAINTAIN_PIPELINE),
        (
            ("run", "--big-kahuna", "--full", "--archive", "--enrich"),
            cli.BIG_KAHUNA_PIPELINE,
        ),
        (("run", "--full", "--archive", "--enrich"), cli.FULL_PIPELINE),
        (("run", "--archive", "--enrich"), cli.ARCHIVE_PIPELINE),
        (("run",), cli.DEFAULT_PIPELINE),
    ),
    ids=(
        "maintain-over-big-kahuna",
        "big-kahuna-over-full-archive-enrich",
        "full-over-archive-enrich",
        "archive-over-enrich",
        "default",
    ),
)
def test_run_pipeline_selection_precedence(
    argv: tuple[str, ...],
    expected_pipeline: object,
) -> None:
    """Selector flag precedence is pure and does not dispatch pipeline work."""
    args = cli._build_parser().parse_args(argv)

    assert cli._select_run_pipeline(args) is expected_pipeline


def test_big_kahuna_direct_guard_retains_a_nonempty_stash_root(tmp_path: Path) -> None:
    """An existing direct caller can retain its pre-resolved Curator stash value."""
    assert not cli._big_kahuna_export_root_missing(
        cli.BIG_KAHUNA_PIPELINE,
        {"curator_export_root": tmp_path / "curator-export"},
    )
    assert cli._big_kahuna_export_root_missing(
        cli.BIG_KAHUNA_PIPELINE,
        {"curator_export_root": ""},
    )


def test_big_kahuna_dry_run_uses_legacy_preview_guard_first(
    disposable_vault,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """P0-05: --dry-run routes through pure planner (no execution attempted)."""
    cli_module = disposable_vault.prepare_legacy_cli(monkeypatch)
    boundary_calls: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "_select_run_pipeline",
        _unexpected_boundary_call("run pipeline selection", boundary_calls),
    )
    monkeypatch.setattr(sys, "argv", ["musaeus", "run", "--big-kahuna", "--dry-run"])

    with pytest.raises(SystemExit) as exited:
        cli_module.main()

    # Preview exits with success (not error)
    assert exited.value.code == 0
    captured = capsys.readouterr()
    # Pure planner outputs a preview, not the legacy error message
    assert "Preview" in captured.out
    assert "Proposed actions" in captured.out
    assert boundary_calls == []  # No execution attempted


def test_big_kahuna_help_marks_the_route_temporarily_unavailable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI help does not advertise the currently blocked route as usable."""
    parser = cli._build_parser()

    with pytest.raises(SystemExit) as exited:
        parser.parse_args(["run", "--help"])

    assert exited.value.code == 0
    help_output = " ".join(capsys.readouterr().out.split())
    assert "Temporarily unavailable: no supported pre-resolved Curator export root" in help_output
