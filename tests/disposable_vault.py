"""Disposable MUSAEUS test vaults and safety guards for P0 regression tests.

Later P0 tests must request the ``disposable_vault`` fixture instead of constructing
paths from a user environment.  It supplies only temporary vault/state/config roots,
blocks protected real paths before a filesystem operation is made, and denies all
in-process network transport.  The guards are installed with pytest's ``monkeypatch``
fixture, so each test restores the host process environment and socket behaviour.
"""

from __future__ import annotations

import builtins
import hashlib
import io
import os
import shutil
import socket
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator
from urllib.parse import unquote, urlparse

import pytest

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch
    from musaeus.config import MusicConfig


# Keep these explicit: a guard must fail closed before an operation reaches a
# known live library, state/configuration root, predecessor project, or NEXUS.
PROTECTED_REAL_ROOTS: tuple[Path, ...] = (
    Path("/home/grey/Music"),
    Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT"),
    Path("/home/grey/.config/musaeus"),
    Path("/home/grey/Projects/ORPHEUS"),
    Path("/mnt/FORGE2TB/Projects/ORPHEUS"),
    Path("/mnt/FORGE2TB/Projects/orpheus"),
    Path("/home/grey/Projects/NEXUS"),
    Path("/mnt/FORGE2TB/Projects/NEXUS"),
    # Block any remaining real-home configuration or state path as well.
    Path("/home/grey"),
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
    """Raised before a test touches a protected or real-user filesystem path."""


class NetworkAccessDenied(RuntimeError):
    """Raised before a test can create, resolve, or send over a network transport."""


def _absolute_path(value: object) -> Path | None:
    """Return an absolute lexical path without opening the target."""
    if isinstance(value, int):
        # File descriptors have no path identity that can be safely verified.
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


@dataclass
class PathGuard:
    """Intercept common filesystem APIs and reject protected paths before I/O."""

    protected_roots: tuple[Path, ...] = PROTECTED_REAL_ROOTS
    blocked_attempts: list[str] = field(default_factory=list)
    _realpath: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.protected_roots = tuple(_absolute_path(root) for root in self.protected_roots)  # type: ignore[arg-type]
        self._realpath = os.path.realpath

    def assert_safe_path(self, value: object, operation: str = "filesystem access") -> None:
        """Fail before ``operation`` can reach a protected path or symlink target."""
        lexical = _absolute_path(value)
        if lexical is None:
            return
        self._raise_if_protected(lexical, operation)

        # ``realpath`` follows only path metadata.  Checking it catches a safe-looking
        # temporary symlink whose target is a protected root before the actual operation.
        resolved = Path(self._realpath(os.fspath(lexical)))
        self._raise_if_protected(resolved, operation)

    def _raise_if_protected(self, candidate: Path, operation: str) -> None:
        for root in self.protected_roots:
            if _is_within(candidate, root):
                detail = f"{operation}: {candidate} resolves inside protected root {root}"
                self.blocked_attempts.append(detail)
                raise UnsafeTestPathError(detail)

    def _reject_dir_fd(self, kwargs: dict[str, object], operation: str) -> None:
        if any(kwargs.get(name) is not None for name in ("dir_fd", "src_dir_fd", "dst_dir_fd")):
            raise UnsafeTestPathError(
                f"{operation}: directory-descriptor paths are rejected because they cannot be verified"
            )

    def install(self, monkeypatch: "MonkeyPatch") -> None:
        """Install per-test wrappers; pytest restores every wrapper automatically."""
        original_path_resolve = Path.resolve
        original_realpath = os.path.realpath

        def guarded_path_resolve(path: Path, strict: bool = False) -> Path:
            self.assert_safe_path(path, "Path.resolve")
            return original_path_resolve(path, strict=strict)

        def guarded_realpath(path: object, *, strict: bool = False) -> str:
            self.assert_safe_path(path, "os.path.realpath")
            result = original_realpath(path, strict=strict)
            self.assert_safe_path(result, "os.path.realpath")
            return result

        monkeypatch.setattr(Path, "resolve", guarded_path_resolve)
        monkeypatch.setattr(os.path, "realpath", guarded_realpath)

        self._patch_open(monkeypatch, builtins, "open")
        self._patch_open(monkeypatch, io, "open")
        for name in (
            "access",
            "chmod",
            "chown",
            "listdir",
            "lstat",
            "mkdir",
            "makedirs",
            "open",
            "readlink",
            "remove",
            "rmdir",
            "scandir",
            "stat",
            "statvfs",
            "truncate",
            "unlink",
            "utime",
            "walk",
        ):
            self._patch_unary_os_call(monkeypatch, name)
        for name in ("link", "rename", "replace", "symlink"):
            self._patch_binary_os_call(monkeypatch, name)
        self._patch_sqlite_connect(monkeypatch)
        self._patch_shutil(monkeypatch)

    def _patch_open(self, monkeypatch: "MonkeyPatch", module: object, name: str) -> None:
        original = getattr(module, name)

        def guarded_open(file: object, *args: object, **kwargs: object) -> Any:
            self.assert_safe_path(file, f"{module.__name__}.{name}")
            return original(file, *args, **kwargs)

        monkeypatch.setattr(module, name, guarded_open)

    def _patch_unary_os_call(self, monkeypatch: "MonkeyPatch", name: str) -> None:
        original = getattr(os, name)

        def guarded(path: object, *args: object, **kwargs: object) -> Any:
            self._reject_dir_fd(kwargs, f"os.{name}")
            self.assert_safe_path(path, f"os.{name}")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(os, name, guarded)

    def _patch_binary_os_call(self, monkeypatch: "MonkeyPatch", name: str) -> None:
        original = getattr(os, name)

        def guarded(source: object, destination: object, *args: object, **kwargs: object) -> Any:
            self._reject_dir_fd(kwargs, f"os.{name}")
            self.assert_safe_path(source, f"os.{name}")
            self.assert_safe_path(destination, f"os.{name}")
            return original(source, destination, *args, **kwargs)

        monkeypatch.setattr(os, name, guarded)

    def _patch_sqlite_connect(self, monkeypatch: "MonkeyPatch") -> None:
        original_connect = sqlite3.connect

        def guarded_connect(database: object, *args: object, **kwargs: object) -> sqlite3.Connection:
            self._assert_safe_sqlite_target(database)
            return original_connect(database, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", guarded_connect)

    def _assert_safe_sqlite_target(self, database: object) -> None:
        if database == ":memory:":
            return
        if isinstance(database, (str, bytes)):
            value = os.fsdecode(database)
            if value.startswith("file:"):
                parsed = urlparse(value)
                if parsed.path in ("", ":memory:"):
                    return
                self.assert_safe_path(unquote(parsed.path), "sqlite3.connect")
                return
        self.assert_safe_path(database, "sqlite3.connect")

    def _patch_shutil(self, monkeypatch: "MonkeyPatch") -> None:
        for name in ("copy", "copy2", "copyfile", "copymode", "copystat", "copytree", "move"):
            original = getattr(shutil, name)

            def guarded_copy(source: object, destination: object, *args: object, _original=original, _name=name, **kwargs: object) -> Any:
                self.assert_safe_path(source, f"shutil.{_name}")
                self.assert_safe_path(destination, f"shutil.{_name}")
                return _original(source, destination, *args, **kwargs)

            monkeypatch.setattr(shutil, name, guarded_copy)

        for name in ("chown", "disk_usage", "rmtree"):
            original = getattr(shutil, name)

            def guarded_path(path: object, *args: object, _original=original, _name=name, **kwargs: object) -> Any:
                self.assert_safe_path(path, f"shutil.{_name}")
                return _original(path, *args, **kwargs)

            monkeypatch.setattr(shutil, name, guarded_path)


@dataclass
class TransportDenial:
    """In-process network kill switch that records and fails every attempted transport."""

    attempts: list[str] = field(default_factory=list)

    def install(self, monkeypatch: "MonkeyPatch") -> None:
        """Patch socket/DNS APIs only for the active pytest test."""

        def deny(operation: str, target: object) -> None:
            detail = f"{operation}: {target!r}"
            self.attempts.append(detail)
            raise NetworkAccessDenied(f"Network access denied by disposable-vault harness ({detail})")

        def denied_create_connection(address: object, *args: object, **kwargs: object) -> None:
            deny("socket.create_connection", address)

        def denied_connect(_socket: socket.socket, address: object) -> None:
            deny("socket.socket.connect", address)

        def denied_connect_ex(_socket: socket.socket, address: object) -> int:
            deny("socket.socket.connect_ex", address)
            raise AssertionError("unreachable")

        def denied_sendto(_socket: socket.socket, data: object, address: object | None = None) -> int:
            deny("socket.socket.sendto", address)
            raise AssertionError("unreachable")

        def denied_lookup(host: object, *args: object, **kwargs: object) -> None:
            deny("socket name lookup", host)

        monkeypatch.setattr(socket, "create_connection", denied_create_connection)
        monkeypatch.setattr(socket.socket, "connect", denied_connect)
        monkeypatch.setattr(socket.socket, "connect_ex", denied_connect_ex)
        monkeypatch.setattr(socket.socket, "sendto", denied_sendto)
        monkeypatch.setattr(socket, "getaddrinfo", denied_lookup)
        monkeypatch.setattr(socket, "gethostbyaddr", denied_lookup)
        monkeypatch.setattr(socket, "gethostbyname", denied_lookup)
        monkeypatch.setattr(socket, "gethostbyname_ex", denied_lookup)


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
class VaultSnapshot:
    """Inventory evidence for a disposable vault before/after comparison."""

    directories: tuple[str, ...]
    file_hashes: tuple[tuple[str, str], ...]
    database_checksum: str | None
    event_count: int | None

    def difference_from(self, before: "VaultSnapshot") -> str:
        """Summarise state changes for an expected-failure baseline assertion."""
        changes: list[str] = []
        if self.directories != before.directories:
            changes.append("directory tree changed")
        if self.file_hashes != before.file_hashes:
            before_files = dict(before.file_hashes)
            after_files = dict(self.file_hashes)
            added = sorted(set(after_files) - set(before_files))
            changed = sorted(
                path for path in set(before_files) & set(after_files) if before_files[path] != after_files[path]
            )
            detail = [*added, *changed]
            changes.append(f"file inventory changed ({', '.join(detail[:5])})")
        if self.database_checksum != before.database_checksum:
            changes.append("database checksum changed")
        if self.event_count != before.event_count:
            changes.append(f"event count {before.event_count!r} -> {self.event_count!r}")
        return "; ".join(changes) if changes else "no managed-state difference"


@dataclass
class DisposableVault:
    """All fixture-only roots and controls needed by consumer-readiness tests."""

    root: Path
    vault_root: Path
    inbox: Path
    staging: Path
    quarantine: Path
    runs_root: Path
    recovery_root: Path
    reports_root: Path
    state_root: Path
    database_path: Path
    home: Path
    xdg_config_home: Path
    home_config_dir: Path
    xdg_config_dir: Path
    path_guard: PathGuard = field(default_factory=PathGuard)
    transport: TransportDenial = field(default_factory=TransportDenial)
    clock: FakeClock = field(default_factory=FakeClock)

    @classmethod
    def create(cls, tmp_path: Path) -> "DisposableVault":
        """Create a complete, empty fixture topology below pytest's ``tmp_path``."""
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
            recovery_root=root / "recovery",
            reports_root=root / "reports",
            state_root=root / "state",
            database_path=root / "state" / "musaeus.db",
            home=home,
            xdg_config_home=xdg_config_home,
            home_config_dir=home / ".config" / "musaeus",
            xdg_config_dir=xdg_config_home / "musaeus",
        )
        for directory in (
            fixture.inbox,
            fixture.staging,
            fixture.quarantine,
            fixture.runs_root,
            fixture.recovery_root,
            fixture.reports_root,
            fixture.state_root,
            fixture.home_config_dir,
            fixture.xdg_config_dir,
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
                f"MUSAEUS_META_DIR={self.vault_root / 'MetaData'}",
                "",
            )
        )
        for config_dir in (self.home_config_dir, self.xdg_config_dir):
            (config_dir / "settings.env").write_text(settings, encoding="utf-8")
            (config_dir / "credentials.env").write_text("", encoding="utf-8")

    def install(self, monkeypatch: "MonkeyPatch") -> "DisposableVault":
        """Activate temporary environment, path guard, and transport denial for one test."""
        self.install_environment(monkeypatch)
        self.path_guard.install(monkeypatch)
        self.transport.install(monkeypatch)
        return self

    def install_environment(self, monkeypatch: "MonkeyPatch") -> None:
        """Point HOME, XDG paths, MUSAEUS roots, and credentials at fixture-only values."""
        for name in _SENSITIVE_ENVIRONMENT:
            monkeypatch.delenv(name, raising=False)
        environment = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.xdg_config_home),
            "XDG_CACHE_HOME": str(self.root / "xdg-cache"),
            "XDG_DATA_HOME": str(self.root / "xdg-data"),
            "XDG_STATE_HOME": str(self.root / "xdg-state"),
            "MUSAEUS_VAULT_ROOT": str(self.vault_root),
            "MUSAEUS_DB_PATH": str(self.database_path),
            "MUSAEUS_INBOX": str(self.inbox),
            "MUSAEUS_STAGING": str(self.staging),
            "MUSAEUS_QUARANTINE": str(self.quarantine),
            "MUSAEUS_RUNS_ROOT": str(self.runs_root),
            "MUSAEUS_META_DIR": str(self.vault_root / "MetaData"),
            "MUSAEUS_RECOVERY_ROOT": str(self.recovery_root),
            "MUSAEUS_REPORTS_ROOT": str(self.reports_root),
            "MUSAEUS_CONFIG_HOME": str(self.xdg_config_home),
        }
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(Path, "home", classmethod(lambda path_cls: path_cls(self.home)))

    def music_config(self) -> "MusicConfig":
        """Resolve the current application config after this fixture's environment is active."""
        from musaeus.config import MusicConfig

        return MusicConfig.from_env()

    def prepare_legacy_cli(self, monkeypatch: "MonkeyPatch") -> Any:
        """Bind cached legacy config/resume paths to this fixture before invoking its CLI."""
        from musaeus import cli
        from musaeus import config as config_module
        from musaeus.setup import wizard

        monkeypatch.setattr(config_module, "_cached_config", None)
        monkeypatch.setattr(config_module, "_USER_CONFIG_DIR", self.home_config_dir)
        monkeypatch.setattr(config_module, "_SETTINGS_FILE", self.home_config_dir / "settings.env")
        monkeypatch.setattr(config_module, "_CREDENTIALS_FILE", self.home_config_dir / "credentials.env")
        monkeypatch.setattr(cli, "_RESUME_FILE", self.home_config_dir / "resume_state.json")
        monkeypatch.setattr(wizard, "_CONFIG_DIR", self.home_config_dir)
        monkeypatch.setattr(wizard, "_SETTINGS_FILE", self.home_config_dir / "settings.env")
        monkeypatch.setattr(wizard, "_CREDENTIALS_FILE", self.home_config_dir / "credentials.env")
        return cli

    def write_inbox_file(self, relative_path: str | Path, content: bytes) -> Path:
        """Create a fixture input file while refusing a path that escapes the disposable inbox."""
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Fixture inbox paths must be relative and contained")
        destination = self.inbox / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return destination

    def snapshot(self) -> VaultSnapshot:
        """Capture directory/file hashes plus SQLite checksum/event evidence under fixture root."""
        directories: list[str] = []
        file_hashes: list[tuple[str, str]] = []
        for path in sorted(self.root.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(self.root).as_posix()
            if path.is_dir():
                directories.append(relative)
            elif path.is_file():
                file_hashes.append((relative, self._sha256(path)))
        return VaultSnapshot(
            directories=tuple(directories),
            file_hashes=tuple(file_hashes),
            database_checksum=self._sha256(self.database_path) if self.database_path.exists() else None,
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
                return int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            finally:
                connection.close()
        except sqlite3.Error:
            return None


@pytest.fixture
def disposable_vault(tmp_path: Path, monkeypatch: "MonkeyPatch") -> Iterator[DisposableVault]:
    """Provide a fully isolated fixture vault with per-test guards already active."""
    fixture = DisposableVault.create(tmp_path).install(monkeypatch)
    yield fixture
