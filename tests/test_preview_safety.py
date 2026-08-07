"""P0-05 regression proof for truthful local-only CLI preview."""

from __future__ import annotations

import sys
from dataclasses import dataclass

import pytest

from musaeus.network_policy import (
    LocalOnlyNetworkPolicyGateway,
    NetworkAccessDenied,
    PreviewNetworkPolicy,
)
from musaeus.planning import CommandRequest, PreviewUsageError, RunMode, build_preview_plan


def _unexpected_boundary(name: str, calls: list[str]):
    def blocked(*_args: object, **_kwargs: object) -> None:
        calls.append(name)
        raise AssertionError(f"{name} must not run during preview")

    return blocked


@dataclass
class RecordingLocalOnlyGateway:
    """Injected test gateway that can prove planning only queries policy facts."""

    preview_policy_calls: int = 0
    dispatches: list[str] | None = None

    def __post_init__(self) -> None:
        if self.dispatches is None:
            self.dispatches = []

    def preview_policy(self) -> PreviewNetworkPolicy:
        self.preview_policy_calls += 1
        return PreviewNetworkPolicy(name="fixture-local-only", external_lookup_permitted=False)

    def dispatch(self, destination: str) -> None:
        assert self.dispatches is not None
        self.dispatches.append(destination)
        raise NetworkAccessDenied("fixture gateway denies every dispatch")


def test_preview_planner_uses_an_injected_local_only_gateway_without_dispatch() -> None:
    """**Validates: Requirements MCR-001, MCR-006**"""
    gateway = RecordingLocalOnlyGateway()

    result = build_preview_plan(
        CommandRequest(command="preview", mode=RunMode.PREVIEW, stage_names=("IngestStage",)),
        network_policy=gateway,
    )

    assert gateway.preview_policy_calls == 1
    assert gateway.dispatches == []
    assert result.network_policy == PreviewNetworkPolicy(
        name="fixture-local-only", external_lookup_permitted=False
    )
    assert result.external_lookup_performed is False
    assert result.managed_state_changed is False


def test_default_preview_gateway_is_local_only_and_denies_dispatch() -> None:
    """**Validates: Requirements MCR-001**"""
    gateway = LocalOnlyNetworkPolicyGateway()

    assert gateway.preview_policy() == PreviewNetworkPolicy(
        name="local_only", external_lookup_permitted=False
    )
    with pytest.raises(NetworkAccessDenied, match="local-only"):
        gateway.dispatch("https://provider.invalid/lookup")


def test_preview_rejects_a_network_permissive_gateway() -> None:
    """**Validates: Requirements MCR-001**"""

    class NetworkPermissiveGateway:
        def preview_policy(self) -> PreviewNetworkPolicy:
            return PreviewNetworkPolicy(name="network-preview", external_lookup_permitted=True)

        def dispatch(self, destination: str) -> None:
            raise AssertionError(f"preview must not dispatch {destination}")

    with pytest.raises(PreviewUsageError, match="local-only"):
        build_preview_plan(
            CommandRequest(command="preview", mode=RunMode.PREVIEW),
            network_policy=NetworkPermissiveGateway(),
        )


def test_preview_command_preserves_disposable_fixture_and_reports_safety(
    disposable_vault,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**Validates: Requirements MCR-001, MCR-006**

    This is the dedicated ``preview`` entry point proof.  The shared CLI route
    matrix covers each registered ``--dry-run`` route and the ``dry-run`` alias.
    """
    disposable_vault.initialise_database()
    disposable_vault.write_inbox_file("artist/preview.flac", b"preview fixture audio")
    disposable_vault.snapshot()  # Stabilise possible SQLite read-only sidecars.
    before = disposable_vault.snapshot()
    boundary_calls: list[str] = []
    cli_module = disposable_vault.prepare_legacy_cli(monkeypatch)
    monkeypatch.setattr(
        cli_module, "_setup_logging", _unexpected_boundary("logging", boundary_calls)
    )
    monkeypatch.setattr(
        cli_module, "get_config", _unexpected_boundary("configuration", boundary_calls)
    )
    monkeypatch.setattr(
        cli_module, "open_db", _unexpected_boundary("writable database", boundary_calls)
    )
    monkeypatch.setattr(sys, "argv", ["musaeus", "preview"])

    with pytest.raises(SystemExit) as exited:
        cli_module.main()

    assert exited.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Scope: unclassified" in captured.out
    assert "Proposed actions: 3" in captured.out
    assert "Action summary:" in captured.out
    assert "Assumptions:" in captured.out
    assert (
        "Safety: No managed state was changed; external lookup was not performed." in captured.out
    )
    assert boundary_calls == []
    after = disposable_vault.snapshot()
    assert after == before, after.difference_from(before)
    assert disposable_vault.transport.attempts == []
    assert disposable_vault.path_guard.write_attempts == []
    assert disposable_vault.subprocesses.attempts == []
