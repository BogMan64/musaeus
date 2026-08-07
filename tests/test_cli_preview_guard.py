"""Fail-closed compatibility coverage for legacy CLI and console preview routes."""

from __future__ import annotations

import argparse
import builtins
import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from musaeus import cli
from musaeus import setup as setup_api
from musaeus.planning import RunMode
from musaeus.preview_guard import (
    LEGACY_PREVIEW_COMMANDS,
    LEGACY_PREVIEW_EXIT_CODE,
    LEGACY_PREVIEW_GUARD_ATTR,
    LEGACY_PREVIEW_MESSAGE,
    LegacyPreviewAction,
)

_DRY_RUN_ROUTES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("run",), ("run", "--dry-run")),
    (("dry-run",), ("dry-run",)),
    (("ingest",), ("ingest", "--dry-run")),
    (("sentinel",), ("sentinel", "--dry-run")),
    (("scholar",), ("scholar", "--dry-run")),
    (("normalize",), ("normalize", "--dry-run")),
    (("forge",), ("forge", "--dry-run")),
    (("tagger",), ("tagger", "--dry-run")),
    (("ghost",), ("ghost", "--dry-run")),
    (("health",), ("health", "--dry-run")),
    (("auditor",), ("auditor", "--dry-run")),
    (("enrich",), ("enrich", "--dry-run")),
    (("mb-enrich",), ("mb-enrich", "--dry-run")),
    (("neardupe",), ("neardupe", "--dry-run")),
    (("rebuild-db",), ("rebuild-db", "--dry-run")),
    (("curator",), ("curator", "--dry-run")),
    (("acousticid",), ("acousticid", "--dry-run")),
    (("transcode",), ("transcode", "--dry-run")),
    (("reviewer",), ("reviewer", "--dry-run")),
    (("integrity",), ("integrity", "--dry-run")),
    (("albumart",), ("albumart", "--dry-run")),
    (("overnight",), ("overnight", "--dry-run")),
    (("playlist",), ("playlist", "--dry-run")),
    (
        ("canon-review", "apply"),
        ("canon-review", "apply", "--fixes", "fixture-fixes.csv", "--dry-run"),
    ),
    (("review", "generate"), ("review", "generate", "--dry-run")),
    (("review", "apply"), ("review", "apply", "--dry-run")),
)
_LEGACY_SCRIPT_PREVIEW_ROUTES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "scripts/musaeus_canon_review.py",
        ("apply", "--fixes", "fixture-fixes.csv", "--dry-run"),
    ),
    ("scripts/musaeus_fix_mislabeled.py", ()),
    ("scripts/resolve_near_dupes.py", ()),
)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PROVIDER_ENVIRONMENT = (
    "ACOUSTICID_API_KEY",
    "DISCOGS_API_KEY",
    "GROQ_API_KEY",
    "LASTFM_API_KEY",
    "MUSICBRAINZ_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
)


def _record_prior_fixture_event(database_path: Path) -> None:
    """Seed a durable event so the guard proves an existing DB is unchanged."""
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "INSERT INTO events (run_id, event_type, note) VALUES (?, ?, ?)",
            ("fixture-prior-run", "FIXTURE_PRIOR", "pre-existing disposable event"),
        )
        connection.commit()
    finally:
        connection.close()


def _parser_dry_run_routes(parser: argparse.ArgumentParser) -> set[tuple[str, ...]]:
    """Return all command paths whose parser metadata declares ``dry_run``."""
    routes: set[tuple[str, ...]] = set()

    def visit(current: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
        if any(action.dest == "dry_run" for action in current._actions):
            routes.add(path)
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    visit(child, (*path, name))

    visit(parser, ())
    return routes


def _parser_dry_run_actions(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    """Return every parser action that exposes the legacy dry-run option."""
    actions: list[argparse.Action] = []

    def visit(current: argparse.ArgumentParser) -> None:
        for action in current._actions:
            if action.dest == "dry_run":
                actions.append(action)
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    visit(child)

    visit(parser)
    return actions


def _registered_legacy_preview_paths(parser: argparse.ArgumentParser) -> list[tuple[str, ...]]:
    """Find accepted command spellings which must explicitly mark the shared guard."""
    paths: list[tuple[str, ...]] = []

    def visit(current: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
        for action in current._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            for name, child in action.choices.items():
                child_path = (*path, name)
                if name in LEGACY_PREVIEW_COMMANDS:
                    paths.append(child_path)
                visit(child, child_path)

    visit(parser, ())
    return paths


def _unexpected_boundary_call(name: str, calls: list[str]):
    def unexpected(*_args: object, **_kwargs: object) -> None:
        calls.append(name)
        raise AssertionError(f"{name} must not run for a blocked legacy preview")

    return unexpected


def _fresh_process_environment(tmp_path: Path) -> tuple[dict[str, str], dict[str, Path]]:
    """Build an isolated child-process environment without creating managed state."""
    root = tmp_path / "fresh-module-preview-guard"
    paths = {
        "root": root,
        "home": root / "home",
        "xdg_config": root / "xdg-config",
        "xdg_cache": root / "xdg-cache",
        "xdg_data": root / "xdg-data",
        "xdg_state": root / "xdg-state",
        "tmp": root / "tmp",
        "config_home": root / "musaeus-config-home",
        "vault": root / "vault",
        "database": root / "state" / "musaeus.db",
        "runs": root / "vault" / "RUNS",
        "recovery": root / "recovery",
        "reports": root / "reports",
    }
    for key in (
        "root",
        "home",
        "xdg_config",
        "xdg_cache",
        "xdg_data",
        "xdg_state",
        "tmp",
        "config_home",
    ):
        paths[key].mkdir(parents=True, exist_ok=True)

    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("MUSAEUS_") or name in _PROVIDER_ENVIRONMENT:
            environment.pop(name, None)
    environment.update(
        {
            "HOME": str(paths["home"]),
            "XDG_CONFIG_HOME": str(paths["xdg_config"]),
            "XDG_CACHE_HOME": str(paths["xdg_cache"]),
            "XDG_DATA_HOME": str(paths["xdg_data"]),
            "XDG_STATE_HOME": str(paths["xdg_state"]),
            "TMPDIR": str(paths["tmp"]),
            "TMP": str(paths["tmp"]),
            "TEMP": str(paths["tmp"]),
            "PYTHONDONTWRITEBYTECODE": "1",
            "MUSAEUS_VAULT_ROOT": str(paths["vault"]),
            "MUSAEUS_DB_PATH": str(paths["database"]),
            "MUSAEUS_INBOX": str(paths["vault"] / "INBOX"),
            "MUSAEUS_STAGING": str(paths["vault"] / "STAGING"),
            "MUSAEUS_QUARANTINE": str(paths["vault"] / "QUARANTINE"),
            "MUSAEUS_RUNS_ROOT": str(paths["runs"]),
            "MUSAEUS_META_DIR": str(paths["vault"] / "MetaData"),
            "MUSAEUS_RECOVERY_ROOT": str(paths["recovery"]),
            "MUSAEUS_REPORTS_ROOT": str(paths["reports"]),
            "MUSAEUS_CONFIG_HOME": str(paths["config_home"]),
            "MUSAEUS_DISABLE_PROJECT_ENV": "1",
        }
    )
    return environment, paths


def _install_child_socket_tripwire(tmp_path: Path, environment: dict[str, str]) -> Path:
    """Install a child-only standard-Python socket tripwire outside managed roots.

    This is deliberately not an OS-level sandbox: it records and rejects connection
    routes reached through the Python ``socket`` module after ``sitecustomize`` loads.
    """
    tripwire_root = tmp_path / "child-socket-tripwire"
    tripwire_root.mkdir(parents=True, exist_ok=True)
    attempts_path = tmp_path / "child-socket-attempts.log"
    (tripwire_root / "sitecustomize.py").write_text(
        """import os
import socket
from pathlib import Path

_log_path = os.environ.get(\"MUSAEUS_CHILD_SOCKET_TRIPWIRE_LOG\")


def _record(entry):
    if _log_path:
        path = Path(_log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open(\"a\", encoding=\"utf-8\") as stream:
            stream.write(entry + \"\\n\")


def _deny(operation, target):
    _record(f\"{operation}: {target!r}\")
    raise RuntimeError(f\"child socket tripwire blocked {operation}: {target!r}\")


def _create_connection(address, *args, **kwargs):
    _deny(\"socket.create_connection\", address)


def _connect(_socket, address):
    _deny(\"socket.socket.connect\", address)


def _connect_ex(_socket, address):
    _deny(\"socket.socket.connect_ex\", address)


def _sendto(_socket, _data, *args, **kwargs):
    target = kwargs.get(\"address\")
    if target is None and args:
        target = args[-1]
    _deny(\"socket.socket.sendto\", target)


_record(\"installed\")
socket.create_connection = _create_connection
socket.socket.connect = _connect
socket.socket.connect_ex = _connect_ex
socket.socket.sendto = _sendto
""",
        encoding="utf-8",
    )
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(tripwire_root), existing_pythonpath) if value
    )
    environment["MUSAEUS_CHILD_SOCKET_TRIPWIRE_LOG"] = str(attempts_path)
    return attempts_path


def _assert_child_socket_tripwire_clear(attempts_path: Path) -> None:
    """Confirm the child loaded the tripwire and made no standard-Python socket attempt."""
    assert attempts_path.read_text(encoding="utf-8") == "installed\n"


def _tree_inventory(root: Path) -> tuple[str, ...]:
    """Return a simple managed-root inventory for fresh-process before/after evidence."""
    return tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*")))


def test_dry_run_route_matrix_matches_parser_metadata() -> None:
    """P0-05: All --dry-run options now route through the pure planner."""
    parser = cli._build_parser()
    covered_routes = {route for route, _ in _DRY_RUN_ROUTES if route != ("dry-run",)}
    assert _parser_dry_run_routes(parser) == covered_routes
    assert ("dry-run",) in {route for route, _ in _DRY_RUN_ROUTES}
    assert _parser_dry_run_actions(parser)
    assert all(
        isinstance(action, cli._PurePreviewAction) for action in _parser_dry_run_actions(parser)
    )


def test_registered_legacy_preview_aliases_opt_into_the_shared_guard() -> None:
    """P0-05: Legacy preview commands are now routed through pure planner."""
    parser = cli._build_parser()
    registered_paths = _registered_legacy_preview_paths(parser)

    assert registered_paths
    for path in registered_paths:
        parsed = parser.parse_args(path)
        # Legacy guard is no longer set; preview routes through pure planner
        assert parsed.run_mode is RunMode.PREVIEW


@pytest.mark.parametrize(
    ("route", "argv"),
    _DRY_RUN_ROUTES,
    ids=[" ".join(route) for route, _ in _DRY_RUN_ROUTES],
)
def test_typed_cli_preview_routes_are_pure_and_local_only(
    disposable_vault,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    route: tuple[str, ...],
    argv: tuple[str, ...],
) -> None:
    """**Validates: Requirements MCR-001, MCR-006**

    Every registered CLI dry-run route is now a typed, stdout-only preview.  The
    fixture snapshot includes file hashes/metadata, database checksum/event count,
    configuration, and the complete directory tree.
    """
    del route
    disposable_vault.initialise_database()
    _record_prior_fixture_event(disposable_vault.database_path)
    disposable_vault.write_inbox_file("artist/fixture-track.flac", b"fixture audio")
    # SQLite can materialise read-only WAL sidecars during a snapshot. Stabilise
    # that fixture-observation detail before taking the CLI before/after baseline.
    disposable_vault.snapshot()
    before = disposable_vault.snapshot()
    assert before.event_count == 1

    cli_module = disposable_vault.prepare_legacy_cli(monkeypatch)
    unexpected_calls: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "_setup_logging",
        _unexpected_boundary_call("logging setup", unexpected_calls),
    )
    monkeypatch.setattr(
        cli_module,
        "get_config",
        _unexpected_boundary_call("configuration resolution", unexpected_calls),
    )
    monkeypatch.setattr(
        cli_module,
        "open_db",
        _unexpected_boundary_call("writable database initialisation", unexpected_calls),
    )
    monkeypatch.setattr(
        cli_module,
        "_run_pipeline",
        _unexpected_boundary_call("pipeline execution", unexpected_calls),
    )
    monkeypatch.setattr(
        setup_api,
        "needs_setup",
        _unexpected_boundary_call("first-run setup check", unexpected_calls),
    )
    monkeypatch.setattr(
        setup_api,
        "run_wizard",
        _unexpected_boundary_call("setup wizard", unexpected_calls),
    )
    monkeypatch.setattr(sys, "argv", ["musaeus", *argv])

    with pytest.raises(SystemExit) as exited:
        cli_module.main()

    assert exited.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Scope: unclassified" in captured.out
    assert "Proposed actions:" in captured.out
    assert "Action summary:" in captured.out
    assert "Assumptions:" in captured.out
    assert (
        "Safety: No managed state was changed; external lookup was not performed." in captured.out
    )
    assert "Network policy: local-only" in captured.out
    assert unexpected_calls == []
    after = disposable_vault.snapshot()
    assert after == before, after.difference_from(before)
    assert disposable_vault.transport.attempts == []
    assert disposable_vault.subprocesses.attempts == []
    assert disposable_vault.path_guard.write_attempts == []


def test_legacy_dry_run_help_explains_the_safety_block(
    disposable_vault,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """P0-05: dry-run command now routes through the pure planner, not blocked."""
    cli_module = disposable_vault.prepare_legacy_cli(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["musaeus", "dry-run", "--help"])

    with pytest.raises(SystemExit) as exited:
        cli_module.main()

    assert exited.value.code == 0
    captured = capsys.readouterr()
    # Preview is no longer blocked; it shows the normal help
    assert "Shows what the command would do without making any changes" in captured.out
    assert captured.err == ""


def test_normal_cli_run_route_still_dispatches_without_the_preview_guard(
    disposable_vault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compatibility block does not turn an ordinary live route into a preview block."""
    cli_module = disposable_vault.prepare_legacy_cli(monkeypatch)
    observed: list[tuple[list[object], bool, dict | None]] = []

    def fake_run_pipeline(stages: list[object], dry_run: bool, stash: dict | None = None) -> int:
        observed.append((stages, dry_run, stash))
        return 0

    monkeypatch.setattr(cli_module, "_setup_logging", lambda _verbose: None)
    monkeypatch.setattr(setup_api, "needs_setup", lambda: False)
    monkeypatch.setattr(cli_module, "_run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(sys, "argv", ["musaeus", "run"])

    with pytest.raises(SystemExit) as exited:
        cli_module.main()

    assert exited.value.code == 0
    assert observed == [(cli_module.DEFAULT_PIPELINE, False, None)]


@pytest.mark.parametrize(
    ("argv", "expected"),
    ((["--help"], "positional arguments:"), (["--version"], "musaeus")),
)
def test_help_and_version_remain_available_without_initialisation(
    disposable_vault,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    expected: str,
) -> None:
    """Parser help/version exits retain their normal successful behaviour."""
    cli_module = disposable_vault.prepare_legacy_cli(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["musaeus", *argv])

    with pytest.raises(SystemExit) as exited:
        cli_module.main()

    assert exited.value.code == 0
    captured = capsys.readouterr()
    assert expected in captured.out
    assert captured.err == ""


def test_console_preview_menu_selection_blocks_without_pipeline_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A public console preview selection terminates before pipeline dispatch."""
    from musaeus.console import Console

    console = Console()
    unexpected_calls: list[str] = []
    monkeypatch.setattr(
        console,
        "_run_pipeline",
        _unexpected_boundary_call("console pipeline dispatch", unexpected_calls),
    )
    monkeypatch.setattr(
        console,
        "_open_db",
        _unexpected_boundary_call("console database initialisation", unexpected_calls),
    )
    monkeypatch.setattr(builtins, "input", lambda _prompt: "1")

    with pytest.raises(SystemExit) as exited:
        console._main_menu()

    assert exited.value.code == LEGACY_PREVIEW_EXIT_CODE
    captured = capsys.readouterr()
    assert "Preview temporarily unavailable" in captured.out
    assert LEGACY_PREVIEW_MESSAGE in captured.out
    assert unexpected_calls == []


def test_console_stage_menu_preview_selection_blocks_before_stage_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A public single-stage preview selection terminates before stage dispatch."""
    from musaeus.console import Console

    console = Console()
    answers = iter(("0", "0"))
    unexpected_calls: list[str] = []
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        console,
        "_run_stage_with_stash",
        _unexpected_boundary_call("console stage dispatch", unexpected_calls),
    )
    monkeypatch.setattr(
        console,
        "_open_db",
        _unexpected_boundary_call("console database initialisation", unexpected_calls),
    )

    with pytest.raises(SystemExit) as exited:
        console._stage_menu()

    assert exited.value.code == LEGACY_PREVIEW_EXIT_CODE
    captured = capsys.readouterr()
    assert "Preview temporarily unavailable" in captured.out
    assert LEGACY_PREVIEW_MESSAGE in captured.out
    assert unexpected_calls == []


@pytest.mark.parametrize("entrypoint", ("pipeline", "stage", "stage-with-stash"))
def test_console_preview_dispatchers_guard_before_config_or_database_work(
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Direct console orchestration calls cannot bypass the shared preview block."""
    from musaeus.console import Console
    from musaeus.stages import IngestStage

    console = Console()
    unexpected_calls: list[str] = []
    monkeypatch.setattr(
        console,
        "_open_db",
        _unexpected_boundary_call("console database initialisation", unexpected_calls),
    )

    if entrypoint == "pipeline":
        exit_code = console._run_pipeline(dry_run=True)
    elif entrypoint == "stage":
        exit_code = console._run_stage(IngestStage, dry_run=True)
    else:
        exit_code = console._run_stage_with_stash(IngestStage, dry_run=True, stash={})

    assert exit_code == LEGACY_PREVIEW_EXIT_CODE
    captured = capsys.readouterr()
    assert LEGACY_PREVIEW_MESSAGE in captured.out
    assert unexpected_calls == []


@pytest.mark.parametrize(
    "argv",
    (("run", "--dry-run"), ("--verbose", "run", "--dry-run"), ("dry-run",)),
    ids=("run", "verbose-run", "alias"),
)
def test_fresh_python_module_preview_routes_are_bounded_to_disposable_paths(
    tmp_path: Path,
    argv: tuple[str, ...],
) -> None:
    """A real child process is safe here because all paths/providers are temporary and disabled.

    This test intentionally does not request ``disposable_vault``: that fixture denies
    subprocess creation, while this regression must verify the actual ``python -m``
    entry point. Its child has HOME, XDG, temporary, and every MUSAEUS path below
    ``tmp_path``; project env loading is disabled, provider values are removed, and
    bytecode writes are disabled.
    """
    environment, paths = _fresh_process_environment(tmp_path)
    socket_attempts = _install_child_socket_tripwire(tmp_path, environment)
    before = _tree_inventory(paths["root"])

    result = subprocess.run(
        [sys.executable, "-m", "musaeus", *argv],
        cwd=_PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert "Scope: unclassified" in result.stdout
    assert "Proposed actions:" in result.stdout
    assert (
        "Safety: No managed state was changed; external lookup was not performed." in result.stdout
    )
    assert "Network policy: local-only" in result.stdout
    _assert_child_socket_tripwire_clear(socket_attempts)
    assert _tree_inventory(paths["root"]) == before
    assert not paths["vault"].exists()
    assert not paths["database"].exists()
    assert not paths["runs"].exists()
    assert not (paths["config_home"] / "musaeus").exists()


@pytest.mark.parametrize(
    ("script", "argv"),
    _LEGACY_SCRIPT_PREVIEW_ROUTES,
    ids=("canon-review", "fix-mislabeled", "resolve-near-dupes"),
)
def test_fresh_legacy_script_preview_routes_fail_before_managed_initialisation(
    tmp_path: Path,
    script: str,
    argv: tuple[str, ...],
) -> None:
    """Direct legacy script previews fail closed in a fresh temporary child process."""
    environment, paths = _fresh_process_environment(tmp_path)
    socket_attempts = _install_child_socket_tripwire(tmp_path, environment)
    before = _tree_inventory(paths["root"])

    result = subprocess.run(
        [sys.executable, str(_PROJECT_ROOT / script), *argv],
        cwd=_PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == LEGACY_PREVIEW_EXIT_CODE
    assert result.stdout == ""
    assert result.stderr == f"{LEGACY_PREVIEW_MESSAGE}\n"
    _assert_child_socket_tripwire_clear(socket_attempts)
    assert _tree_inventory(paths["root"]) == before
    assert not paths["vault"].exists()
    assert not paths["database"].exists()
    assert not paths["runs"].exists()
    assert not (paths["config_home"] / "musaeus").exists()


def test_resolve_near_dupes_import_defers_configuration_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing the resolver alone does not resolve MUSAEUS configuration."""
    from musaeus import config as config_module

    calls: list[str] = []

    def unexpected_config(*_args: object, **_kwargs: object) -> object:
        calls.append("get_config")
        raise AssertionError("resolver import must not resolve MUSAEUS configuration")

    monkeypatch.setattr(config_module, "get_config", unexpected_config)
    module_name = "_resolve_near_dupes_import_probe"
    spec = importlib.util.spec_from_file_location(
        module_name, _PROJECT_ROOT / "scripts" / "resolve_near_dupes.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)

    spec.loader.exec_module(module)

    assert calls == []
    assert not hasattr(module, "DB_PATH")


def test_cli_pipeline_orchestrator_blocks_direct_preview_before_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A direct call to the CLI orchestration boundary cannot bypass argparse."""
    unexpected_calls: list[str] = []
    monkeypatch.setattr(
        cli,
        "get_config",
        _unexpected_boundary_call("configuration resolution", unexpected_calls),
    )
    monkeypatch.setattr(
        cli,
        "open_db",
        _unexpected_boundary_call("database initialisation", unexpected_calls),
    )

    assert cli._run_pipeline([], dry_run=True) == LEGACY_PREVIEW_EXIT_CODE

    captured = capsys.readouterr()
    assert captured.err == f"{LEGACY_PREVIEW_MESSAGE}\n"
    assert unexpected_calls == []


@pytest.mark.parametrize(
    "helper",
    ("rebuild", "review-generate", "review-apply", "canon-review"),
)
def test_direct_user_facing_preview_helpers_block_before_all_managed_boundaries(
    helper: str,
    disposable_vault,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Direct preview helpers share the same early block as parser-routed commands."""
    from musaeus import approval as approval_module
    from musaeus import config as config_module
    from musaeus import db as db_module
    from musaeus import rebuild as rebuild_module

    disposable_vault.initialise_database()
    _record_prior_fixture_event(disposable_vault.database_path)
    disposable_vault.snapshot()
    before = disposable_vault.snapshot()
    boundary_calls: list[str] = []

    monkeypatch.setattr(
        config_module,
        "get_config",
        _unexpected_boundary_call("configuration resolution", boundary_calls),
    )
    monkeypatch.setattr(
        db_module,
        "open_db",
        _unexpected_boundary_call("database initialisation", boundary_calls),
    )
    monkeypatch.setattr(
        db_module,
        "log_event",
        _unexpected_boundary_call("event write", boundary_calls),
    )
    monkeypatch.setattr(
        Path,
        "mkdir",
        _unexpected_boundary_call("directory creation", boundary_calls),
    )
    monkeypatch.setattr(
        cli,
        "get_config",
        _unexpected_boundary_call("configuration resolution", boundary_calls),
    )
    monkeypatch.setattr(
        cli,
        "open_db",
        _unexpected_boundary_call("database initialisation", boundary_calls),
    )

    if helper == "rebuild":
        exit_code = rebuild_module.cmd_rebuild_db(dry_run=True)
    elif helper == "review-generate":
        exit_code = approval_module.cmd_review_generate(dry_run=True)
    elif helper == "review-apply":
        exit_code = approval_module.cmd_review_apply(dry_run=True)
    else:
        exit_code = cli._cmd_canon_review(mode="apply", dry_run=True)

    assert exit_code == LEGACY_PREVIEW_EXIT_CODE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{LEGACY_PREVIEW_MESSAGE}\n"
    assert boundary_calls == []
    after = disposable_vault.snapshot()
    assert after == before, after.difference_from(before)
    assert disposable_vault.path_guard.write_attempts == []


@pytest.mark.parametrize("argv", ((), ("console",)), ids=("bare", "console"))
def test_cli_console_routes_defer_logging_and_setup_until_console_action(
    argv: tuple[str, ...],
    disposable_vault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare and explicit console routes do not initialise anything before menu selection."""
    from musaeus import console as console_module

    cli_module = disposable_vault.prepare_legacy_cli(monkeypatch)
    started: list[str] = []
    logging_calls: list[bool] = []
    logging_setups: list[object] = []
    unexpected_calls: list[str] = []

    class FakeConsole:
        def __init__(self, configure_logging) -> None:
            logging_setups.append(configure_logging)

        def run(self) -> None:
            started.append("console")

    monkeypatch.setattr(console_module, "Console", FakeConsole)
    monkeypatch.setattr(cli_module, "_setup_logging", logging_calls.append)
    monkeypatch.setattr(
        setup_api,
        "needs_setup",
        _unexpected_boundary_call("first-run setup check", unexpected_calls),
    )
    monkeypatch.setattr(
        setup_api,
        "run_wizard",
        _unexpected_boundary_call("setup wizard", unexpected_calls),
    )
    monkeypatch.setattr(sys, "argv", ["musaeus", *argv])

    cli_module.main()

    assert started == ["console"]
    assert len(logging_setups) == 1
    assert logging_calls == []
    assert unexpected_calls == []


@pytest.mark.parametrize("argv", ((), ("console",)), ids=("bare", "console"))
def test_cli_console_preview_selection_does_not_configure_any_logging(
    argv: tuple[str, ...],
    disposable_vault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real CLI console preview selection reaches neither CLI nor console logging."""
    from musaeus import console as console_module

    cli_module = disposable_vault.prepare_legacy_cli(monkeypatch)
    unexpected_calls: list[str] = []
    answers = iter(("1", "10"))
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        cli_module,
        "_setup_logging",
        _unexpected_boundary_call("CLI logging setup", unexpected_calls),
    )
    monkeypatch.setattr(
        console_module.logging,
        "basicConfig",
        _unexpected_boundary_call("console logging setup", unexpected_calls),
    )
    monkeypatch.setattr(
        console_module.Console,
        "_boot_check",
        _unexpected_boundary_call("console boot configuration", unexpected_calls),
    )
    monkeypatch.setattr(sys, "argv", ["musaeus", *argv])

    with pytest.raises(SystemExit) as exited:
        cli_module.main()

    assert exited.value.code == LEGACY_PREVIEW_EXIT_CODE
    assert next(answers) == "10"
    assert unexpected_calls == []


def test_cli_console_logging_callback_runs_only_when_console_requests_it(
    disposable_vault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI passes logging into console mode without invoking it during dispatch."""
    from musaeus import console as console_module

    cli_module = disposable_vault.prepare_legacy_cli(monkeypatch)
    logging_calls: list[bool] = []

    class FakeConsole:
        def __init__(self, configure_logging) -> None:
            self._configure_logging = configure_logging

        def run(self) -> None:
            # Models a user selecting a non-preview console action after the menu appears.
            self._configure_logging()

    monkeypatch.setattr(console_module, "Console", FakeConsole)
    monkeypatch.setattr(cli_module, "_setup_logging", logging_calls.append)
    monkeypatch.setattr(sys, "argv", ["musaeus", "--verbose", "console"])

    cli_module.main()

    assert logging_calls == [True]


def test_console_run_blocks_preview_before_boot_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A console preview selection terminates before boot or logging begins."""
    from musaeus import console as console_module
    from musaeus.console import Console

    console = Console()
    answers = iter(("1", "10"))
    unexpected_calls: list[str] = []
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        console,
        "_boot_check",
        _unexpected_boundary_call("console boot configuration", unexpected_calls),
    )
    monkeypatch.setattr(
        console_module.logging,
        "basicConfig",
        _unexpected_boundary_call("console logging setup", unexpected_calls),
    )

    with pytest.raises(SystemExit) as exited:
        console.run()

    assert exited.value.code == LEGACY_PREVIEW_EXIT_CODE
    captured = capsys.readouterr()
    assert "Preview temporarily unavailable" in captured.out
    assert LEGACY_PREVIEW_MESSAGE in captured.out
    assert next(answers) == "10"
    assert unexpected_calls == []


def test_console_single_stage_preview_selection_blocks_before_boot_or_logging(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real ``3 → 0 → 0 → 10`` route exits before any managed boundary."""
    from musaeus import console as console_module
    from musaeus.console import Console

    console = Console()
    answers = iter(("3", "0", "0", "10"))
    unexpected_calls: list[str] = []
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(answers))
    for name, label in (
        ("_boot_check", "console boot configuration"),
        ("_run_stage_with_stash", "console stage dispatch"),
    ):
        monkeypatch.setattr(console, name, _unexpected_boundary_call(label, unexpected_calls))
    for name, label in (
        ("get_config", "configuration resolution"),
        ("open_db", "database initialisation"),
        ("ffmpeg_available", "ffmpeg probe"),
        ("ffprobe_available", "ffprobe probe"),
    ):
        monkeypatch.setattr(
            console_module, name, _unexpected_boundary_call(label, unexpected_calls)
        )
    monkeypatch.setattr(
        console_module.logging,
        "basicConfig",
        _unexpected_boundary_call("console logging setup", unexpected_calls),
    )

    with pytest.raises(SystemExit) as exited:
        console.run()

    assert exited.value.code == LEGACY_PREVIEW_EXIT_CODE
    captured = capsys.readouterr()
    assert "Preview temporarily unavailable" in captured.out
    assert LEGACY_PREVIEW_MESSAGE in captured.out
    assert next(answers) == "10"
    assert unexpected_calls == []


def test_console_live_stage_selection_boots_and_configures_logging_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live stage action still configures logging and boots immediately before dispatch."""
    from musaeus import console as console_module
    from musaeus.console import Console
    from musaeus.stages import IngestStage

    console = Console()
    answers = iter(("3", "0", "1", "10"))
    boot_calls: list[str] = []
    logging_calls: list[dict[str, object]] = []
    dispatches: list[tuple[type, bool, dict | None]] = []

    def boot() -> bool:
        boot_calls.append("boot")
        console._boot_ready = True
        return True

    def dispatch(stage_cls: type, dry_run: bool, stash: dict | None = None) -> None:
        dispatches.append((stage_cls, dry_run, stash))

    monkeypatch.setattr(builtins, "input", lambda _prompt: next(answers))
    monkeypatch.setattr(console, "_boot_check", boot)
    monkeypatch.setattr(console, "_run_stage_with_stash", dispatch)
    monkeypatch.setattr(
        console_module.logging,
        "basicConfig",
        lambda **kwargs: logging_calls.append(kwargs),
    )

    console.run()

    assert boot_calls == ["boot"]
    assert len(logging_calls) == 1
    assert logging_calls[0]["level"] == console_module.logging.WARNING
    assert dispatches == [(IngestStage, False, {})]


@pytest.mark.parametrize(
    ("module", "argv"),
    (("musaeus", ()), ("musaeus", ("console",)), ("musaeus.console", ())),
    ids=("bare", "console", "console-module"),
)
def test_fresh_console_single_stage_preview_entrypoints_block_without_managed_boot_work(
    tmp_path: Path,
    module: str,
    argv: tuple[str, ...],
) -> None:
    """Exercise ``3 → 0 → 0 → 10`` in fresh console children on temporary paths only.

    The child-side ``sitecustomize`` tripwire rejects standard-Python socket routes;
    it is evidence for this test, not an operating-system network sandbox.
    """
    environment, paths = _fresh_process_environment(tmp_path)
    socket_attempts = _install_child_socket_tripwire(tmp_path, environment)
    before = _tree_inventory(paths["root"])

    result = subprocess.run(
        [sys.executable, "-m", module, *argv],
        cwd=_PROJECT_ROOT,
        env=environment,
        input="3\n0\n0\n10\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == LEGACY_PREVIEW_EXIT_CODE
    assert "Preview temporarily unavailable" in result.stdout
    assert LEGACY_PREVIEW_MESSAGE in result.stdout
    assert result.stderr == ""
    _assert_child_socket_tripwire_clear(socket_attempts)
    assert _tree_inventory(paths["root"]) == before
    assert not paths["vault"].exists()
    assert not paths["database"].exists()
    assert not paths["runs"].exists()
    assert not (paths["config_home"] / "musaeus").exists()
