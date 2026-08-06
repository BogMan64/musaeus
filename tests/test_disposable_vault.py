"""Focused safety tests for the disposable-vault P0 fixture harness."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

from tests.disposable_vault import (
    PROTECTED_REAL_ROOTS,
    NetworkAccessDenied,
    TransportDenial,
    UnsafeTestPathError,
)


def test_disposable_vault_uses_only_temporary_roots(disposable_vault) -> None:
    """All current and future P0 inputs resolve beneath pytest's temporary directory."""
    roots = (
        disposable_vault.vault_root,
        disposable_vault.inbox,
        disposable_vault.staging,
        disposable_vault.quarantine,
        disposable_vault.runs_root,
        disposable_vault.recovery_root,
        disposable_vault.reports_root,
        disposable_vault.state_root,
        disposable_vault.home,
        disposable_vault.xdg_config_home,
    )
    for root in roots:
        assert root.is_relative_to(disposable_vault.root)
        assert root.exists()

    assert Path.home() == disposable_vault.home
    assert Path(os.environ["XDG_CONFIG_HOME"]) == disposable_vault.xdg_config_home
    config = disposable_vault.music_config()
    assert config.vault_root == disposable_vault.vault_root
    assert config.db_path == disposable_vault.database_path


def test_snapshot_detects_fixture_inventory_and_database_changes(disposable_vault) -> None:
    before = disposable_vault.snapshot()
    disposable_vault.write_inbox_file("Artist/Album/track.flac", b"fixture audio bytes")

    after_file = disposable_vault.snapshot()
    assert after_file != before
    assert "vault/INBOX/Artist/Album/track.flac" in dict(after_file.file_hashes)
    assert after_file.event_count is None


def test_path_guard_blocks_all_protected_roots_before_filesystem_access(disposable_vault) -> None:
    for root in PROTECTED_REAL_ROOTS:
        with pytest.raises(UnsafeTestPathError, match="protected root"):
            (root / "blocked-by-test-guard").exists()

    assert len(disposable_vault.path_guard.blocked_attempts) == len(PROTECTED_REAL_ROOTS)


def test_path_guard_prevents_real_config_even_with_temporary_home(disposable_vault) -> None:
    assert Path.home() == disposable_vault.home
    assert Path(os.environ["HOME"]) == disposable_vault.home

    with pytest.raises(UnsafeTestPathError, match="protected root"):
        Path("/home/grey/.config/musaeus/settings.env").read_text(encoding="utf-8")


def test_transport_denial_records_and_blocks_connection_without_network(disposable_vault) -> None:
    with pytest.raises(NetworkAccessDenied, match="Network access denied"):
        socket.create_connection(("example.invalid", 443))

    assert disposable_vault.transport.attempts == [
        "socket.create_connection: ('example.invalid', 443)"
    ]


def test_transport_denial_restores_socket_apis_after_its_scope() -> None:
    original_create_connection = socket.create_connection
    with pytest.MonkeyPatch.context() as isolated_patch:
        denial = TransportDenial()
        denial.install(isolated_patch)
        with pytest.raises(NetworkAccessDenied):
            socket.create_connection(("example.invalid", 443))
        assert denial.attempts

    assert socket.create_connection is original_create_connection


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "P0-01 baseline: legacy `run --dry-run` opens a writable DB and records run/stage events. "
        "P0-05 must make preview observational, then remove or replace this baseline."
    ),
)
def test_legacy_run_dry_run_has_no_managed_state_side_effects_baseline(
    disposable_vault, monkeypatch
) -> None:
    """Characterise the current defect only inside the guarded disposable vault.

    The expected failure is deliberately strict: a future passing result must be
    reviewed rather than silently normalising the current unsafe preview behaviour.
    Transport access and non-zero exits fail normally, not as baseline evidence.
    """
    cli = disposable_vault.prepare_legacy_cli(monkeypatch)
    before = disposable_vault.snapshot()
    monkeypatch.setattr(sys, "argv", ["musaeus", "run", "--dry-run"])

    with pytest.raises(SystemExit) as exited:
        cli.main()

    if exited.value.code != 0:
        pytest.fail(f"Disposable legacy dry-run exited {exited.value.code!r}")
    if disposable_vault.transport.attempts:
        pytest.fail(f"Default dry-run attempted transport: {disposable_vault.transport.attempts}")

    after = disposable_vault.snapshot()
    assert after == before, (
        "Legacy dry-run changed disposable managed state: "
        f"{after.difference_from(before)}"
    )
