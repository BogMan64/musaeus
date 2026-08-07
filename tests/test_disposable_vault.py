"""Focused safety tests for the disposable-vault P0 fixture harness."""

from __future__ import annotations

import builtins
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.disposable_vault import (
    PROTECTED_REAL_ROOTS,
    NetworkAccessDenied,
    PathGuard,
    SubprocessAccessDenied,
    SubprocessDenial,
    TransportDenial,
    UnsafeTestPathError,
)


def test_disposable_vault_uses_only_temporary_roots(disposable_vault) -> None:
    """All P0 inputs and configuration roots remain below this session's temp root."""
    roots = (
        disposable_vault.vault_root,
        disposable_vault.inbox,
        disposable_vault.staging,
        disposable_vault.quarantine,
        disposable_vault.runs_root,
        disposable_vault.meta_dir,
        disposable_vault.recovery_root,
        disposable_vault.reports_root,
        disposable_vault.state_root,
        disposable_vault.home,
        disposable_vault.xdg_config_home,
        disposable_vault.xdg_cache_home,
        disposable_vault.xdg_data_home,
        disposable_vault.xdg_state_home,
        disposable_vault.tmp_dir,
    )
    session_root = Path(os.environ["MUSAEUS_TEST_SESSION_ROOT"])
    for root in roots:
        assert root.is_relative_to(disposable_vault.root)
        assert root.is_relative_to(session_root)
        assert root.exists()

    assert Path.home() == disposable_vault.home
    assert Path(os.environ["XDG_CONFIG_HOME"]) == disposable_vault.xdg_config_home
    assert os.environ["MUSAEUS_DISABLE_PROJECT_ENV"] == "1"
    config = disposable_vault.music_config()
    assert config.vault_root == disposable_vault.vault_root
    assert config.db_path == disposable_vault.database_path
    assert config.meta_dir == disposable_vault.meta_dir


def test_snapshot_captures_fixture_inventory_metadata_and_removed_paths(disposable_vault) -> None:
    payload = disposable_vault.write_inbox_file("Artist/Album/track.flac", b"fixture audio bytes")
    payload.chmod(0o640)
    link = disposable_vault.inbox / "Artist" / "Album" / "track-link.flac"
    link.symlink_to(payload)

    before = disposable_vault.snapshot()
    entries = {entry.path: entry for entry in before.entries}
    assert entries["vault/INBOX/Artist/Album/track.flac"].entry_type == "file"
    assert entries["vault/INBOX/Artist/Album/track.flac"].mode == 0o640
    assert entries["vault/INBOX/Artist/Album/track.flac"].content_hash is not None
    assert entries["vault/INBOX/Artist/Album/track-link.flac"].entry_type == "symlink"
    assert entries["vault/INBOX/Artist/Album/track-link.flac"].link_target == str(payload)
    assert "vault/INBOX/Artist/Album/track.flac" in dict(before.file_hashes)
    assert before.event_count is None

    payload.unlink()
    after = disposable_vault.snapshot()
    assert "removed paths (vault/INBOX/Artist/Album/track.flac)" in after.difference_from(before)


def test_snapshot_fails_loudly_when_an_existing_database_has_no_events_table(
    disposable_vault,
) -> None:
    connection = sqlite3.connect(disposable_vault.database_path)
    connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="could not be read or event-counted"):
        disposable_vault.snapshot()


def test_path_guard_blocks_all_protected_roots_before_filesystem_access(disposable_vault) -> None:
    for root in PROTECTED_REAL_ROOTS:
        with pytest.raises(UnsafeTestPathError, match="protected root"):
            disposable_vault.path_guard.assert_safe_path(
                root / "blocked-by-test-guard", "focused protected-root check"
            )

    assert len(disposable_vault.path_guard.blocked_attempts) == len(PROTECTED_REAL_ROOTS)


def test_path_guard_allows_read_only_paths_outside_fixture_root(tmp_path: Path) -> None:
    """The tripwire does not act as a global read sandbox for pytest/import activity."""
    outside = tmp_path.parent / f"{tmp_path.name}-read-only.txt"
    outside.write_text("disposable read-only evidence", encoding="utf-8")
    try:
        with pytest.MonkeyPatch.context() as isolated_patch:
            path_guard = PathGuard(tmp_path)
            path_guard.install(isolated_patch)
            assert outside.read_text(encoding="utf-8") == "disposable read-only evidence"
            assert path_guard.blocked_attempts == []
    finally:
        outside.unlink(missing_ok=True)


def test_path_guard_blocks_symlinked_writes_that_escape_fixture_root(disposable_vault) -> None:
    """Write containment checks both the lexical path and its resolved symlink target."""
    outside = disposable_vault.root.parent / "outside-through-symlink"
    escape_link = disposable_vault.root / "escape-link"
    escape_link.symlink_to(outside)

    with pytest.raises(UnsafeTestPathError, match="outside disposable fixture root"):
        (escape_link / "blocked.txt").write_text("must not persist", encoding="utf-8")

    assert not outside.exists()


def test_path_guard_contains_writes_but_allows_disposable_writes(disposable_vault) -> None:
    allowed = disposable_vault.root / "allowed-write.txt"
    allowed.write_text("fixture only", encoding="utf-8")
    assert allowed.read_text(encoding="utf-8") == "fixture only"

    outside = disposable_vault.root.parent / "outside-disposable-vault.txt"
    with pytest.raises(UnsafeTestPathError, match="outside disposable fixture root"):
        outside.write_text("must not persist", encoding="utf-8")
    assert not outside.exists()
    assert any(
        "outside disposable fixture root" in attempt
        for attempt in disposable_vault.path_guard.write_attempts
    )


def test_path_guard_prevents_real_config_even_with_temporary_home(disposable_vault) -> None:
    assert Path.home() == disposable_vault.home
    assert Path(os.environ["HOME"]) == disposable_vault.home

    with pytest.raises(UnsafeTestPathError, match="protected root"):
        disposable_vault.path_guard.assert_safe_path(
            Path("/home/grey/.config/musaeus/settings.env"), "focused protected-config check"
        )


def test_transport_denial_records_and_blocks_connection_without_network(disposable_vault) -> None:
    with pytest.raises(NetworkAccessDenied, match="Network access denied"):
        socket.create_connection(("example.invalid", 443))

    assert disposable_vault.transport.attempts == [
        "socket.create_connection: ('example.invalid', 443)"
    ]


def test_transport_denial_blocks_send_routes_with_correct_sendto_signature(
    disposable_vault,
) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(NetworkAccessDenied):
            sock.send(b"payload")
        with pytest.raises(NetworkAccessDenied):
            sock.sendall(b"payload")
        with pytest.raises(NetworkAccessDenied):
            sock.sendto(b"payload", 0, ("127.0.0.1", 9))
    finally:
        sock.close()

    assert disposable_vault.transport.attempts == [
        "socket.socket.send: None",
        "socket.socket.sendall: None",
        "socket.socket.sendto: ('127.0.0.1', 9)",
    ]


def test_subprocess_denial_records_and_blocks_before_child_execution(disposable_vault) -> None:
    with pytest.raises(SubprocessAccessDenied, match="Subprocess access denied"):
        subprocess.run([sys.executable, "-c", "raise SystemExit(99)"], check=True)

    assert disposable_vault.subprocesses.attempts == [
        f"subprocess.run: {[sys.executable, '-c', 'raise SystemExit(99)']!r}"
    ]


def test_guards_restore_python_apis_after_their_scope(tmp_path) -> None:
    original_open = builtins.open
    original_popen = subprocess.Popen
    original_create_connection = socket.create_connection
    with pytest.MonkeyPatch.context() as isolated_patch:
        path_guard = PathGuard(tmp_path)
        transport = TransportDenial()
        processes = SubprocessDenial()
        path_guard.install(isolated_patch)
        transport.install(isolated_patch)
        processes.install(isolated_patch)

        with pytest.raises(NetworkAccessDenied):
            socket.create_connection(("example.invalid", 443))
        with pytest.raises(SubprocessAccessDenied):
            subprocess.run(["not-executed"])
        with pytest.raises(UnsafeTestPathError, match="outside disposable fixture root"):
            (tmp_path.parent / "outside.txt").write_text("blocked", encoding="utf-8")

    assert builtins.open is original_open
    assert subprocess.Popen is original_popen
    assert socket.create_connection is original_create_connection


# P0-01 historical characterisation (replaced by P0-02 positive guard coverage):
# before the compatibility guard, `run --dry-run` appended RUN_START, three
# STAGE_COMPLETE events (ingest, sentinel, scholar), and RUN_END to the fixture
# database. The command is intentionally blocked before that legacy path until
# P0-04/P0-05 implement a truthful preview.
