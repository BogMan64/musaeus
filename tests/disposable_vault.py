"""Disposable MUSAEUS test vaults and fixture-containment tripwires.

This helper is deliberately a test tripwire, not an operating-system sandbox. The
session bootstrap plus lazy configuration resolution prevent MUSAEUS configuration
from reaching live user paths before application modules are imported. Tests that
request ``disposable_vault`` additionally confine Python-level writes to a fixture
root and block standard Python subprocess and in-process network routes. Ordinary
read-only pytest, import, and plugin activity remains outside this guard; native
extensions and unpatched APIs are outside its guarantee.
"""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import io
import os
import shutil
import socket
import sqlite3
import stat
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlparse

import pytest

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch

    from musaeus.config import MusicConfig


# These explicit live locations are never opened by guarded operations: every
# attempted protected-path check fails before the underlying filesystem call. Do
# not protect the whole home directory: pytest plugins and imports may legitimately
# read interpreter-managed files from it during result reporting.
PROTECTED_REAL_ROOTS: tuple[Path, ...] = (
    Path("/home/grey/Music"),
    Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT"),
    Path("/home/grey/.config/musaeus"),
    Path("/home/grey/Projects/ORPHEUS"),
    Path("/mnt/FORGE2TB/Projects/ORPHEUS"),
    Path("/mnt/FORGE2TB/Projects/orpheus"),
    Path("/home/grey/Projects/NEXUS"),
    Path("/mnt/FORGE2TB/Projects/NEXUS"),
)

_SENSITIVE_ENVIRONMENT = (
    "GROQ_API_KEY",
    "LASTFM_API_KEY",
    "OPENROUTER_API_KEY",
    "ACOUSTICID_API_KEY",
    "MUSICBRAINZ_API_KEY",
    "DISCOGS_API_KEY",
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
)


class UnsafeTestPathError(RuntimeError):
    """Raised before a test touches a protected or non-disposable write path."""


class NetworkAccessDenied(RuntimeError):
    """Raised before a test can create, resolve, or send over an in-process transport."""


class SubprocessAccessDenied(RuntimeError):
    """Raised before a fixture test can launch or replace the current process."""


def _absolute_path(value: object) -> Path | None:
    """Return an absolute lexical path without opening the target."""
    if isinstance(value, int):
        return None
    try:
        raw_path = os.fspath(value)
    except TypeError:
        return None
    raw_text = os.fsdecode(raw_path)
    return Path(os.path.abspath(os.path.expanduser(raw_text)))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _write_mode(mode: object) -> bool:
    return isinstance(mode, str) and any(flag in mode for flag in ("w", "a", "x", "+"))


def _open_flags_write(flags: int) -> bool:
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
    temporary_flag = getattr(os, "O_TMPFILE", 0)
    return bool(flags & (write_flags | temporary_flag))


@dataclass
class PathGuard:
    """Guard protected roots and confine path-based writes to one fixture root.

    Reads outside the fixture root remain possible so ordinary imports and stdlib
    access continue to work. Mutating path APIs must resolve within ``fixture_root``;
    the exact ``/dev/null`` device is the sole system-write exception because it
    cannot persist state. File-descriptor-only and native-extension operations remain
    outside this monkeypatch-based guard's guarantee.
    """

    fixture_root: Path
    protected_roots: tuple[Path, ...] = PROTECTED_REAL_ROOTS
    blocked_attempts: list[str] = field(default_factory=list)
    write_attempts: list[str] = field(default_factory=list)
    _realpath: Any = field(init=False, repr=False)
    _thread_state: threading.local = field(default_factory=threading.local, init=False, repr=False)

    def __post_init__(self) -> None:
        fixture_root = _absolute_path(self.fixture_root)
        if fixture_root is None:
            raise TypeError("fixture_root must be a filesystem path")
        self.fixture_root = fixture_root
        roots = tuple(_absolute_path(root) for root in self.protected_roots)
        self.protected_roots = tuple(root for root in roots if root is not None)
        self._realpath = os.path.realpath

    def _is_resolving(self) -> bool:
        return bool(getattr(self._thread_state, "realpath_depth", 0))

    def _path_candidates(self, value: object, operation: str) -> tuple[Path, Path] | None:
        lexical = _absolute_path(value)
        if lexical is None:
            return None
        self._raise_if_protected(lexical, operation)

        depth = getattr(self._thread_state, "realpath_depth", 0)
        self._thread_state.realpath_depth = depth + 1
        try:
            resolved = Path(self._realpath(os.fspath(lexical)))
        finally:
            self._thread_state.realpath_depth = depth
        self._raise_if_protected(resolved, operation)
        return lexical, resolved

    def assert_safe_path(self, value: object, operation: str = "filesystem access") -> None:
        """Reject a protected lexical path or symlink target before underlying I/O."""
        self._path_candidates(value, operation)

    def assert_writable_path(self, value: object, operation: str) -> None:
        """Reject a write unless both lexical and resolved paths stay in the fixture."""
        candidates = self._path_candidates(value, operation)
        if candidates is None:
            detail = f"{operation}: path cannot be verified for a contained write"
            self.blocked_attempts.append(detail)
            self.write_attempts.append(detail)
            raise UnsafeTestPathError(detail)
        for candidate in candidates:
            if candidate == Path("/dev/null"):
                continue
            if not _is_within(candidate, self.fixture_root):
                detail = (
                    f"{operation}: {candidate} is outside disposable fixture root "
                    f"{self.fixture_root}"
                )
                self.blocked_attempts.append(detail)
                self.write_attempts.append(detail)
                raise UnsafeTestPathError(detail)

    def _raise_if_protected(self, candidate: Path, operation: str) -> None:
        for root in self.protected_roots:
            if _is_within(candidate, root):
                detail = f"{operation}: {candidate} resolves inside protected root {root}"
                self.blocked_attempts.append(detail)
                raise UnsafeTestPathError(detail)

    def _reject_dir_fd(self, kwargs: dict[str, object], operation: str) -> None:
        if any(kwargs.get(name) is not None for name in ("dir_fd", "src_dir_fd", "dst_dir_fd")):
            detail = f"{operation}: directory-descriptor paths cannot be verified"
            self.blocked_attempts.append(detail)
            self.write_attempts.append(detail)
            raise UnsafeTestPathError(detail)

    def install(self, monkeypatch: MonkeyPatch) -> None:
        """Install per-test write/process tripwires; pytest restores them after the test."""
        self._patch_open(monkeypatch, builtins, "open")
        self._patch_open(monkeypatch, io, "open")
        for name in (
            "chmod",
            "chown",
            "mkdir",
            "makedirs",
            "remove",
            "rmdir",
            "truncate",
            "unlink",
            "utime",
        ):
            self._patch_unary_os_call(monkeypatch, name, writes=True)
        self._patch_os_open(monkeypatch)
        for name in ("link", "rename", "replace"):
            self._patch_binary_os_call(monkeypatch, name, source_writes=True)
        self._patch_binary_os_call(monkeypatch, "symlink", source_writes=False)
        self._patch_sqlite_connect(monkeypatch)
        self._patch_shutil(monkeypatch)

    def _patch_open(self, monkeypatch: MonkeyPatch, module: object, name: str) -> None:
        original = getattr(module, name)

        def guarded_open(file: object, *args: object, **kwargs: object) -> Any:
            mode = kwargs.get("mode", args[0] if args else "r")
            if _write_mode(mode):
                self.assert_writable_path(file, f"{module.__name__}.{name}")
            return original(file, *args, **kwargs)

        monkeypatch.setattr(module, name, guarded_open)

    def _patch_unary_os_call(self, monkeypatch: MonkeyPatch, name: str, *, writes: bool) -> None:
        original = getattr(os, name)

        def guarded(path: object, *args: object, **kwargs: object) -> Any:
            operation = f"os.{name}"
            self._reject_dir_fd(kwargs, operation)
            if writes:
                self.assert_writable_path(path, operation)
            return original(path, *args, **kwargs)

        monkeypatch.setattr(os, name, guarded)

    def _patch_os_open(self, monkeypatch: MonkeyPatch) -> None:
        original = os.open

        def guarded_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            if _open_flags_write(flags):
                operation = "os.open"
                self._reject_dir_fd(kwargs, operation)
                self.assert_writable_path(path, operation)
            return original(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", guarded_open)

    def _patch_binary_os_call(
        self, monkeypatch: MonkeyPatch, name: str, *, source_writes: bool
    ) -> None:
        original = getattr(os, name)

        def guarded(source: object, destination: object, *args: object, **kwargs: object) -> Any:
            operation = f"os.{name}"
            self._reject_dir_fd(kwargs, operation)
            if source_writes:
                self.assert_writable_path(source, operation)
            else:
                self.assert_safe_path(source, operation)
            self.assert_writable_path(destination, operation)
            return original(source, destination, *args, **kwargs)

        monkeypatch.setattr(os, name, guarded)

    def _patch_sqlite_connect(self, monkeypatch: MonkeyPatch) -> None:
        original_connect = sqlite3.connect

        def guarded_connect(
            database: object, *args: object, **kwargs: object
        ) -> sqlite3.Connection:
            self._assert_safe_sqlite_target(database, kwargs)
            return original_connect(database, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", guarded_connect)

    def _assert_safe_sqlite_target(self, database: object, kwargs: dict[str, object]) -> None:
        if database == ":memory:":
            return
        if isinstance(database, (str, bytes)):
            value = os.fsdecode(database)
            if value.startswith("file:"):
                parsed = urlparse(value)
                if parsed.path in ("", ":memory:"):
                    return
                target = unquote(parsed.path)
                modes = parse_qs(parsed.query).get("mode", [])
                if modes == ["ro"]:
                    return
                self.assert_writable_path(target, "sqlite3.connect")
                return
        self.assert_writable_path(database, "sqlite3.connect")

    def _patch_shutil(self, monkeypatch: MonkeyPatch) -> None:
        for name in ("copy", "copy2", "copyfile", "copytree"):
            original = getattr(shutil, name)

            def guarded_copy(
                source: object,
                destination: object,
                *args: object,
                _original: Any = original,
                _name: str = name,
                **kwargs: object,
            ) -> Any:
                self.assert_safe_path(source, f"shutil.{_name}")
                self.assert_writable_path(destination, f"shutil.{_name}")
                return _original(source, destination, *args, **kwargs)

            monkeypatch.setattr(shutil, name, guarded_copy)

        for name in ("copymode", "copystat"):
            original = getattr(shutil, name)

            def guarded_metadata(
                source: object,
                destination: object,
                *args: object,
                _original: Any = original,
                _name: str = name,
                **kwargs: object,
            ) -> Any:
                self.assert_safe_path(source, f"shutil.{_name}")
                self.assert_writable_path(destination, f"shutil.{_name}")
                return _original(source, destination, *args, **kwargs)

            monkeypatch.setattr(shutil, name, guarded_metadata)

        original_move = shutil.move

        def guarded_move(
            source: object, destination: object, *args: object, **kwargs: object
        ) -> Any:
            self.assert_writable_path(source, "shutil.move")
            self.assert_writable_path(destination, "shutil.move")
            return original_move(source, destination, *args, **kwargs)

        monkeypatch.setattr(shutil, "move", guarded_move)

        for name, writes in (("chown", True), ("rmtree", True)):
            original = getattr(shutil, name)

            def guarded_path(
                path: object,
                *args: object,
                _original: Any = original,
                _name: str = name,
                _writes: bool = writes,
                **kwargs: object,
            ) -> Any:
                operation = f"shutil.{_name}"
                if _writes:
                    self.assert_writable_path(path, operation)
                else:
                    self.assert_safe_path(path, operation)
                return _original(path, *args, **kwargs)

            monkeypatch.setattr(shutil, name, guarded_path)


@dataclass
class TransportDenial:
    """Record and reject common in-process Python transport routes.

    This deliberately makes no claim to block native transports or networking in a
    child process. Fixture tests also install ``SubprocessDenial`` so standard Python
    subprocess routes fail before a child can be created.
    """

    attempts: list[str] = field(default_factory=list)

    def install(self, monkeypatch: MonkeyPatch) -> None:
        """Patch socket and DNS APIs for the active pytest test only."""

        def deny(operation: str, target: object) -> None:
            detail = f"{operation}: {target!r}"
            self.attempts.append(detail)
            raise NetworkAccessDenied(
                f"Network access denied by disposable-vault harness ({detail})"
            )

        def denied_create_connection(address: object, *args: object, **kwargs: object) -> None:
            deny("socket.create_connection", address)

        def denied_connect(_socket: socket.socket, address: object) -> None:
            deny("socket.socket.connect", address)

        def denied_connect_ex(_socket: socket.socket, address: object) -> int:
            deny("socket.socket.connect_ex", address)
            raise AssertionError("unreachable")

        def denied_send(
            _socket: socket.socket, data: object, *args: object, **kwargs: object
        ) -> int:
            del data, args, kwargs
            deny("socket.socket.send", None)
            raise AssertionError("unreachable")

        def denied_sendall(
            _socket: socket.socket, data: object, *args: object, **kwargs: object
        ) -> None:
            del data, args, kwargs
            deny("socket.socket.sendall", None)

        def denied_sendto(
            _socket: socket.socket, data: object, *args: object, **kwargs: object
        ) -> int:
            del data
            target = kwargs.get("address")
            if target is None and args:
                # socket.sendto(data, address) and socket.sendto(data, flags, address)
                target = args[-1]
            deny("socket.socket.sendto", target)
            raise AssertionError("unreachable")

        def denied_lookup(host: object, *args: object, **kwargs: object) -> None:
            del args, kwargs
            deny("socket name lookup", host)

        monkeypatch.setattr(socket, "create_connection", denied_create_connection)
        monkeypatch.setattr(socket.socket, "connect", denied_connect)
        monkeypatch.setattr(socket.socket, "connect_ex", denied_connect_ex)
        monkeypatch.setattr(socket.socket, "send", denied_send)
        monkeypatch.setattr(socket.socket, "sendall", denied_sendall)
        monkeypatch.setattr(socket.socket, "sendto", denied_sendto)
        if hasattr(socket.socket, "sendmsg"):
            monkeypatch.setattr(socket.socket, "sendmsg", denied_send)
        if hasattr(socket.socket, "sendfile"):
            monkeypatch.setattr(socket.socket, "sendfile", denied_send)
        monkeypatch.setattr(socket, "getaddrinfo", denied_lookup)
        monkeypatch.setattr(socket, "gethostbyaddr", denied_lookup)
        monkeypatch.setattr(socket, "gethostbyname", denied_lookup)
        monkeypatch.setattr(socket, "gethostbyname_ex", denied_lookup)


@dataclass
class SubprocessDenial:
    """Record and reject standard Python subprocess launch routes before execution."""

    attempts: list[str] = field(default_factory=list)

    def install(self, monkeypatch: MonkeyPatch) -> None:
        """Patch common process-launch APIs for the active fixture test."""

        def deny(operation: str, command: object) -> None:
            detail = f"{operation}: {command!r}"
            self.attempts.append(detail)
            raise SubprocessAccessDenied(
                f"Subprocess access denied by disposable-vault harness ({detail})"
            )

        def denied_popen(command: object, *args: object, **kwargs: object) -> Any:
            del args, kwargs
            deny("subprocess.Popen", command)
            raise AssertionError("unreachable")

        def denied_subprocess(command: object, *args: object, _name: str, **kwargs: object) -> Any:
            del args, kwargs
            deny(f"subprocess.{_name}", command)
            raise AssertionError("unreachable")

        def denied_os_process(command: object, *args: object, _name: str, **kwargs: object) -> Any:
            del args, kwargs
            deny(f"os.{_name}", command)
            raise AssertionError("unreachable")

        async def denied_async(command: object, *args: object, _name: str, **kwargs: object) -> Any:
            del args, kwargs
            deny(f"asyncio.{_name}", command)
            raise AssertionError("unreachable")

        monkeypatch.setattr(subprocess, "Popen", denied_popen)
        for name in ("run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"):
            monkeypatch.setattr(
                subprocess,
                name,
                lambda command, *args, _name=name, **kwargs: denied_subprocess(
                    command, *args, _name=_name, **kwargs
                ),
            )
        for name in ("system", "popen"):
            monkeypatch.setattr(
                os,
                name,
                lambda command, *args, _name=name, **kwargs: denied_os_process(
                    command, *args, _name=_name, **kwargs
                ),
            )
        for name in (
            "execl",
            "execle",
            "execlp",
            "execlpe",
            "execv",
            "execve",
            "execvp",
            "execvpe",
            "spawnl",
            "spawnle",
            "spawnlp",
            "spawnlpe",
            "spawnv",
            "spawnve",
            "spawnvp",
            "spawnvpe",
        ):
            if hasattr(os, name):
                monkeypatch.setattr(
                    os,
                    name,
                    lambda command, *args, _name=name, **kwargs: denied_os_process(
                        command, *args, _name=_name, **kwargs
                    ),
                )
        for name in ("create_subprocess_exec", "create_subprocess_shell"):
            monkeypatch.setattr(
                asyncio,
                name,
                lambda command, *args, _name=name, **kwargs: denied_async(
                    command, *args, _name=_name, **kwargs
                ),
            )


@dataclass
class FakeClock:
    """Simple deterministic clock for later cancellation/timeout P0 fixture tests."""

    current: datetime = field(default_factory=lambda: datetime(2024, 1, 1, tzinfo=UTC))

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> datetime:
        self.current += timedelta(seconds=seconds)
        return self.current


@dataclass(frozen=True)
class SnapshotEntry:
    """Portable metadata for one filesystem entry in a disposable snapshot."""

    path: str
    entry_type: str
    mode: int
    content_hash: str | None = None
    link_target: str | None = None


@dataclass(frozen=True)
class VaultSnapshot:
    """Inventory, metadata, and database evidence for a disposable vault."""

    entries: tuple[SnapshotEntry, ...]
    database_checksum: str | None
    event_count: int | None

    @property
    def directories(self) -> tuple[str, ...]:
        """Compatibility view of tracked directory paths."""
        return tuple(entry.path for entry in self.entries if entry.entry_type == "directory")

    @property
    def file_hashes(self) -> tuple[tuple[str, str], ...]:
        """Compatibility view of regular-file content hashes."""
        return tuple(
            (entry.path, entry.content_hash)
            for entry in self.entries
            if entry.entry_type == "file" and entry.content_hash is not None
        )

    def difference_from(self, before: VaultSnapshot) -> str:
        """Summarise additions, removals, and metadata changes for diagnostics."""
        changes: list[str] = []
        before_entries = {entry.path: entry for entry in before.entries}
        after_entries = {entry.path: entry for entry in self.entries}
        removed = sorted(set(before_entries) - set(after_entries))
        added = sorted(set(after_entries) - set(before_entries))
        if removed:
            changes.append(f"removed paths ({self._format_paths(removed)})")
        if added:
            changes.append(f"added paths ({self._format_paths(added)})")

        type_changed = []
        mode_changed = []
        content_changed = []
        link_changed = []
        for path in sorted(set(before_entries) & set(after_entries)):
            old = before_entries[path]
            new = after_entries[path]
            if old.entry_type != new.entry_type:
                type_changed.append(path)
            if old.mode != new.mode:
                mode_changed.append(path)
            if old.content_hash != new.content_hash:
                content_changed.append(path)
            if old.link_target != new.link_target:
                link_changed.append(path)
        if type_changed:
            changes.append(f"entry types changed ({self._format_paths(type_changed)})")
        if mode_changed:
            changes.append(f"modes changed ({self._format_paths(mode_changed)})")
        if content_changed:
            changes.append(f"content hashes changed ({self._format_paths(content_changed)})")
        if link_changed:
            changes.append(f"symlink targets changed ({self._format_paths(link_changed)})")
        if self.database_checksum != before.database_checksum:
            changes.append("database checksum changed")
        if self.event_count != before.event_count:
            changes.append(f"event count {before.event_count!r} -> {self.event_count!r}")
        return "; ".join(changes) if changes else "no managed-state difference"

    @staticmethod
    def _format_paths(paths: list[str]) -> str:
        visible = ", ".join(paths[:5])
        return f"{visible}, …" if len(paths) > 5 else visible


@dataclass
class DisposableVault:
    """All fixture-only roots and controls needed by consumer-readiness tests."""

    root: Path
    vault_root: Path
    inbox: Path
    staging: Path
    quarantine: Path
    runs_root: Path
    meta_dir: Path
    recovery_root: Path
    reports_root: Path
    state_root: Path
    database_path: Path
    home: Path
    xdg_config_home: Path
    xdg_cache_home: Path
    xdg_data_home: Path
    xdg_state_home: Path
    tmp_dir: Path
    home_config_dir: Path
    xdg_config_dir: Path
    path_guard: PathGuard = field(init=False)
    transport: TransportDenial = field(default_factory=TransportDenial)
    subprocesses: SubprocessDenial = field(default_factory=SubprocessDenial)
    clock: FakeClock = field(default_factory=FakeClock)

    def __post_init__(self) -> None:
        self.path_guard = PathGuard(self.root)

    @classmethod
    def create(cls, tmp_path: Path) -> DisposableVault:
        """Create a complete empty topology below a disposable fixture parent."""
        root = tmp_path / "disposable-musaeus"
        vault_root = root / "vault"
        home = root / "home"
        xdg_config_home = root / "xdg-config"
        fixture = cls(
            root=root,
            vault_root=vault_root,
            inbox=vault_root / "INBOX",
            staging=vault_root / "STAGING",
            quarantine=vault_root / "QUARANTINE",
            runs_root=vault_root / "RUNS",
            meta_dir=vault_root / "MetaData",
            recovery_root=root / "recovery",
            reports_root=root / "reports",
            state_root=root / "state",
            database_path=root / "state" / "musaeus.db",
            home=home,
            xdg_config_home=xdg_config_home,
            xdg_cache_home=root / "xdg-cache",
            xdg_data_home=root / "xdg-data",
            xdg_state_home=root / "xdg-state",
            tmp_dir=root / "tmp",
            home_config_dir=home / ".config" / "musaeus",
            xdg_config_dir=xdg_config_home / "musaeus",
        )
        for directory in (
            fixture.vault_root,
            fixture.inbox,
            fixture.staging,
            fixture.quarantine,
            fixture.runs_root,
            fixture.meta_dir,
            fixture.recovery_root,
            fixture.reports_root,
            fixture.state_root,
            fixture.home_config_dir,
            fixture.xdg_config_dir,
            fixture.xdg_cache_home,
            fixture.xdg_data_home,
            fixture.xdg_state_home,
            fixture.tmp_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        fixture._write_temporary_settings()
        return fixture

    def _write_temporary_settings(self) -> None:
        settings = "\n".join(
            (
                f"MUSAEUS_VAULT_ROOT={self.vault_root}",
                f"MUSAEUS_DB_PATH={self.database_path}",
                f"MUSAEUS_INBOX={self.inbox}",
                f"MUSAEUS_STAGING={self.staging}",
                f"MUSAEUS_QUARANTINE={self.quarantine}",
                f"MUSAEUS_RUNS_ROOT={self.runs_root}",
                f"MUSAEUS_META_DIR={self.meta_dir}",
                "",
            )
        )
        for config_dir in (self.home_config_dir, self.xdg_config_dir):
            (config_dir / "settings.env").write_text(settings, encoding="utf-8")
            (config_dir / "credentials.env").write_text("", encoding="utf-8")

    def install(self, monkeypatch: MonkeyPatch) -> DisposableVault:
        """Activate safe paths plus per-test filesystem, transport, and process guards."""
        self.install_environment(monkeypatch)
        self.path_guard.install(monkeypatch)
        self.transport.install(monkeypatch)
        self.subprocesses.install(monkeypatch)
        return self

    def install_environment(self, monkeypatch: MonkeyPatch) -> None:
        """Point all home/XDG/MUSAEUS values at this fixture and remove provider values."""
        for name in _SENSITIVE_ENVIRONMENT:
            monkeypatch.delenv(name, raising=False)
        environment = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.xdg_config_home),
            "XDG_CACHE_HOME": str(self.xdg_cache_home),
            "XDG_DATA_HOME": str(self.xdg_data_home),
            "XDG_STATE_HOME": str(self.xdg_state_home),
            "TMPDIR": str(self.tmp_dir),
            "TMP": str(self.tmp_dir),
            "TEMP": str(self.tmp_dir),
            "MUSAEUS_VAULT_ROOT": str(self.vault_root),
            "MUSAEUS_DB_PATH": str(self.database_path),
            "MUSAEUS_INBOX": str(self.inbox),
            "MUSAEUS_STAGING": str(self.staging),
            "MUSAEUS_QUARANTINE": str(self.quarantine),
            "MUSAEUS_RUNS_ROOT": str(self.runs_root),
            "MUSAEUS_META_DIR": str(self.meta_dir),
            "MUSAEUS_RECOVERY_ROOT": str(self.recovery_root),
            "MUSAEUS_REPORTS_ROOT": str(self.reports_root),
            "MUSAEUS_CONFIG_HOME": str(self.xdg_config_home),
            "MUSAEUS_DISABLE_PROJECT_ENV": "1",
        }
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(tempfile, "tempdir", str(self.tmp_dir))
        monkeypatch.setattr(Path, "home", classmethod(lambda path_cls: path_cls(self.home)))

        # A prior session/fixture cache may contain paths from a different
        # disposable root. Resolve it afresh only after this environment is active.
        from musaeus.config import reset_config_cache

        reset_config_cache()

    def music_config(self) -> MusicConfig:
        """Resolve application configuration after this fixture's environment is active."""
        from musaeus.config import MusicConfig

        return MusicConfig.from_env()

    def prepare_legacy_cli(self, monkeypatch: MonkeyPatch) -> Any:
        """Bind legacy cached paths to this fixture before invoking the existing CLI."""
        from musaeus import cli
        from musaeus import config as config_module
        from musaeus.setup import wizard

        config_module.reset_config_cache()
        monkeypatch.setattr(cli, "_RESUME_FILE", self.xdg_config_dir / "resume_state.json")
        monkeypatch.setattr(wizard, "_CONFIG_DIR", self.xdg_config_dir)
        monkeypatch.setattr(wizard, "_SETTINGS_FILE", self.xdg_config_dir / "settings.env")
        monkeypatch.setattr(wizard, "_CREDENTIALS_FILE", self.xdg_config_dir / "credentials.env")
        return cli

    def initialise_database(self) -> None:
        """Create only the fixture database/schema needed by the legacy baseline."""
        from musaeus.db import open_db

        connection = open_db(self.database_path)
        connection.close()

    def write_inbox_file(self, relative_path: str | Path, content: bytes) -> Path:
        """Create fixture input while refusing a path that escapes the disposable inbox."""
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Fixture inbox paths must be relative and contained")
        destination = self.inbox / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return destination

    def snapshot(self) -> VaultSnapshot:
        """Capture entry types, modes, content evidence, and existing SQLite state."""
        entries: list[SnapshotEntry] = []
        for path in sorted(self.root.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(self.root).as_posix()
            metadata = path.lstat()
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                entries.append(
                    SnapshotEntry(
                        path=relative,
                        entry_type="symlink",
                        mode=mode,
                        link_target=os.readlink(path),
                    )
                )
            elif stat.S_ISDIR(metadata.st_mode):
                entries.append(SnapshotEntry(path=relative, entry_type="directory", mode=mode))
            elif stat.S_ISREG(metadata.st_mode):
                entries.append(
                    SnapshotEntry(
                        path=relative,
                        entry_type="file",
                        mode=mode,
                        content_hash=self._sha256(path),
                    )
                )
            else:
                entries.append(SnapshotEntry(path=relative, entry_type="other", mode=mode))
        return VaultSnapshot(
            entries=tuple(entries),
            database_checksum=self._sha256(self.database_path)
            if self.database_path.exists()
            else None,
            event_count=self._event_count(),
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _event_count(self) -> int | None:
        if not self.database_path.exists():
            return None
        try:
            connection = sqlite3.connect(f"{self.database_path.as_uri()}?mode=ro", uri=True)
            try:
                row = connection.execute("SELECT COUNT(*) FROM events").fetchone()
                if row is None:
                    raise RuntimeError("events count query returned no row")
                return int(row[0])
            finally:
                connection.close()
        except (RuntimeError, sqlite3.Error) as exc:
            raise RuntimeError(
                "Existing disposable database could not be read or event-counted"
            ) from exc


@pytest.fixture
def disposable_vault(tmp_path: Path, monkeypatch: MonkeyPatch) -> Iterator[DisposableVault]:
    """Provide an isolated session-root vault with guards already active."""
    session_root = os.environ.get("MUSAEUS_TEST_SESSION_ROOT")
    if not session_root:
        raise RuntimeError("MUSAEUS test session bootstrap was not installed")
    fixture_parent = Path(session_root) / "fixture-vaults" / uuid.uuid4().hex
    fixture_parent.mkdir(parents=True, exist_ok=False)
    fixture = DisposableVault.create(fixture_parent).install(monkeypatch)
    try:
        yield fixture
    finally:
        # Reset while this fixture's disposable environment still exists. The
        # monkeypatch fixture restores the session environment after this finalizer.
        from musaeus.config import reset_config_cache

        reset_config_cache()
