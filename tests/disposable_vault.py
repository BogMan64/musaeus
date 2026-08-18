"""
MUSAEUS — Disposable-vault fixture harness (P0-01)

Reusable, importable test-safety API. This module contains no test_*
functions itself — it is the harness other test files (and tests/conftest.py)
import from. See tests/conftest.py for how this is wired into every test
session, and tests/test_disposable_vault.py for the isolation tests that
prove the guards below actually work.

What this module provides (P0-01 deliverables):
  - PROTECTED_REAL_ROOTS: the real, named paths that must never be touched
    by a test, in any way (read, write, mkdir, rename, delete, DB connect).
  - PathGuard: an installable sys.addaudithook()-based guard that raises
    RealPathAccessError the instant any code under test performs a
    filesystem/DB operation whose path falls under a protected root.
  - TransportDenialHarness: an installable guard that raises
    NetworkAccessDeniedError the instant any code under test attempts an
    outbound socket connection (covers urllib.request, http.client, and
    any other stdlib networking, since all of them ultimately call
    socket.socket.connect()).
  - FakeClock: a small controllable/advanceable clock utility. Nothing in
    the current musaeus package accepts an injected clock yet (RunContext
    and db.py both call datetime.now()/SQLite datetime('now') directly),
    so this is forward-looking scaffolding for later P0 tasks (P0-09
    cancellation timing, P0-10 lock heartbeats) rather than something
    wired into production code today. Tests can still use it directly for
    their own time-dependent assertions.
  - DisposableVault: a dataclass bundling an isolated vault directory, a
    MusicConfig pointed entirely inside it, a disposable "config home"
    (stands in for a redirected HOME/XDG base), and disposable
    recovery/report roots. The recovery/report roots are plain empty
    directories today — no P0 module reads/writes them yet (state/schema.py,
    safety/recovery.py etc. don't exist in this repo) — they exist so a
    later P0 task has an isolated target to point at without inventing its
    own convention.
  - make_disposable_vault() / open_disposable_db() / new_disposable_context():
    convenience constructors so tests don't hand-roll a MusicConfig every
    time (mirrors the cfg/ctx/ctx_dry fixture pattern already used by
    tests/test_ingest.py etc., just centralised).
  - snapshot_vault_state(): captures file inventory + hashes, a directory
    tree listing, DB event count, and a DB content checksum (via a stable
    iterdump() hash rather than a raw byte hash, so WAL/page-layout churn
    with identical logical content doesn't register as a false diff) —
    for before/after equality comparisons.

Safety note: PROTECTED_REAL_ROOTS is a deny-list of specific, real,
named paths (Grey's actual music library, actual MUSAEUS config/vault
paths), not a strict allow-list of "only tmp_path is permitted". A
strict allow-list would also flag unrelated, harmless operations (pytest's
own cache, coverage's .coverage file, __pycache__ writes, stdlib imports)
that have nothing to do with the safety property this task cares about.
"""

from __future__ import annotations

import hashlib
import os
import socket
import sqlite3
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db

# ── Protected real paths ──────────────────────────────────────────────────────
#
# These are Grey's actual, real paths — not test fixtures, not disposable.
# Nothing under these roots may ever be created, read, renamed, or deleted
# by a test. Kept as a short, explicit, named list (per-path rationale in
# comments) rather than a broad prefix like "all of /home/grey", so the
# guard stays scoped to the specific safety property this task cares about
# and doesn't false-positive on unrelated legitimate filesystem activity.
PROTECTED_REAL_ROOTS: tuple[str, ...] = (
    "/home/grey/Music",  # the real ~470GB personal music library
    "/home/grey/.config/musaeus",  # real settings.env / credentials.env / resume_state.json
    "/mnt/FORGE2TB/Projects/MUSAEUS_VAULT",  # real live working vault (musaeus.db, INBOX, ALAC-Library)
    "/mnt/FORGE2TB/Projects/MUSAEUS_TEST_VAULT",  # Grey's manual/persistent test vault — not disposable-per-test
    "/home/grey/Projects/MUSAEUS_RECOVERY",  # future-only recovery root; spec says never create/probe this
)


def _is_under_protected_root(path_str: str) -> str | None:
    """Return the protected root *path_str* falls under, or None."""
    try:
        resolved = os.path.abspath(path_str)
    except (TypeError, ValueError):
        return None
    for root in PROTECTED_REAL_ROOTS:
        if resolved == root or resolved.startswith(root + os.sep):
            return root
    return None


class RealPathAccessError(RuntimeError):
    """Raised by PathGuard when guarded code touches a protected real path."""


class NetworkAccessDeniedError(RuntimeError):
    """Raised by TransportDenialHarness when guarded code attempts a connection."""


# ── Path guard ─────────────────────────────────────────────────────────────────

# Audit-hook event names that carry a filesystem or DB path as their first
# (or, for os.rename/os.replace, first two) argument(s). Confirmed empirically
# against CPython 3.11 (see P0-01 session notes) — os.mkdir/open/rename/
# replace/remove/rmdir/unlink and sqlite3.connect all fire with a real path
# string/PathLike as an early positional arg; shutil.move/copyfile fire their
# own distinct events in addition to (not instead of) the lower-level events
# their implementation calls internally.
_GUARDED_SINGLE_PATH_EVENTS = frozenset(
    {
        "open",
        "os.mkdir",
        "os.rmdir",
        "os.remove",
        "os.unlink",
        "os.listdir",
        "os.scandir",
        "sqlite3.connect",
        "shutil.rmtree",
        "shutil.copyfile",
    }
)
_GUARDED_DUAL_PATH_EVENTS = frozenset(
    {
        "os.rename",
        "os.replace",
        "shutil.move",
    }
)


class PathGuard:
    """
    Installable audit-hook guard: raises RealPathAccessError the moment any
    guarded filesystem/DB operation targets a path under PROTECTED_REAL_ROOTS.

    Usage:
        guard = PathGuard()
        guard.install()   # safe to call once per process; see note below
        try:
            ...
        finally:
            guard.disable()  # NOT uninstall() — see note below

    Important: CPython does not support removing an audit hook once added
    (sys.addaudithook has no counterpart). install() therefore adds the
    hook at most once per process (guarded by _installed) and all
    enable/disable state lives on the PathGuard instance itself, checked
    from inside the hook callback on every event. Calling install() again
    on a second PathGuard instance would stack a second hook and double
    the per-event overhead across the whole test session, so tests/
    conftest.py creates exactly one PathGuard for the session and every
    fixture that wants guarding reuses it via enable()/disable(), rather
    than constructing additional instances.
    """

    def __init__(self) -> None:
        self.enabled: bool = False
        self.attempts: list[tuple[str, str, str]] = []  # (event, path, matched_root)
        self._installed: bool = False
        self._lock = threading.Lock()

    def _check(self, path_str: str, event: str) -> None:
        root = _is_under_protected_root(path_str)
        if root is not None:
            with self._lock:
                self.attempts.append((event, path_str, root))
            raise RealPathAccessError(
                f"blocked {event} under protected real path {path_str!r} "
                f"(matched protected root {root!r}). This test attempted to "
                f"touch real MUSAEUS data — use the disposable_vault fixture "
                f"instead."
            )

    def _hook(self, event: str, args: tuple[Any, ...]) -> None:
        if not self.enabled:
            return
        if event in _GUARDED_SINGLE_PATH_EVENTS:
            if args:
                self._check(str(args[0]), event)
        elif event in _GUARDED_DUAL_PATH_EVENTS:
            for a in args[:2]:
                if a is not None:
                    self._check(str(a), event)

    def install(self) -> None:
        """Add the audit hook. Idempotent — safe to call more than once."""
        if self._installed:
            return
        sys.addaudithook(self._hook)
        self._installed = True

    def enable(self) -> None:
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False

    def reset_attempts(self) -> None:
        with self._lock:
            self.attempts.clear()

    @contextmanager
    def active(self) -> Iterator[PathGuard]:
        """Context manager: enable for the block, restore previous state after."""
        previous = self.enabled
        self.install()
        self.enable()
        try:
            yield self
        finally:
            self.enabled = previous


# ── Transport-denial harness ──────────────────────────────────────────────────


class TransportDenialHarness:
    """
    Installable guard: raises NetworkAccessDeniedError the instant guarded
    code opens an outbound socket connection. Patches socket.socket.connect
    and socket.socket.connect_ex directly (a monkeypatch, not an audit
    hook) because raising from inside an audit-hook callback for
    'socket.connect' would still let a caller's except-and-retry logic
    treat the resulting OSError-family exception as "the network is just
    unreachable" — several stages in this codebase already catch broad
    Exception around network calls (mb_enrich._search_artist,
    acousticid._acousticid_lookup) and would silently swallow that.
    Patching connect() directly raises the SAME distinctive
    NetworkAccessDeniedError those broad excepts would still catch, but
    every attempt is recorded here BEFORE it can be swallowed, so a test
    can assert on harness.attempts regardless of how the calling code
    handles the exception.

    Usage:
        harness = TransportDenialHarness()
        with harness.active():
            ...  # any urlopen/socket.connect call raises + is recorded
    """

    def __init__(self) -> None:
        self.attempts: list[tuple[str, Any]] = []  # (method_name, address)
        self._installed: bool = False
        self._orig_connect: Callable[..., Any] | None = None
        self._orig_connect_ex: Callable[..., Any] | None = None
        self._lock = threading.Lock()

    def _denied_connect(self, sock: socket.socket, address: Any) -> None:
        with self._lock:
            self.attempts.append(("connect", address))
        raise NetworkAccessDeniedError(
            f"blocked outbound network connection attempt to {address!r}. "
            f"Default policy is local-only / no network in tests — see "
            f"MCR-001/MCR-006. Use the disposable_vault fixture's transport "
            f"harness if a test genuinely needs to exercise this path."
        )

    def _denied_connect_ex(self, sock: socket.socket, address: Any) -> int:
        with self._lock:
            self.attempts.append(("connect_ex", address))
        raise NetworkAccessDeniedError(
            f"blocked outbound network connection attempt (connect_ex) to {address!r}."
        )

    def install(self) -> None:
        """Monkeypatch socket.socket.connect/connect_ex. Idempotent."""
        if self._installed:
            return
        self._orig_connect = socket.socket.connect
        self._orig_connect_ex = socket.socket.connect_ex
        harness = self

        def _patched_connect(self_sock: socket.socket, address: Any) -> None:
            return harness._denied_connect(self_sock, address)

        def _patched_connect_ex(self_sock: socket.socket, address: Any) -> int:
            return harness._denied_connect_ex(self_sock, address)

        socket.socket.connect = _patched_connect  # type: ignore[method-assign,assignment]
        socket.socket.connect_ex = _patched_connect_ex  # type: ignore[method-assign,assignment]
        self._installed = True

    def uninstall(self) -> None:
        """Restore the original socket.socket.connect/connect_ex."""
        if not self._installed:
            return
        assert self._orig_connect is not None
        assert self._orig_connect_ex is not None
        socket.socket.connect = self._orig_connect  # type: ignore[method-assign,assignment]
        socket.socket.connect_ex = self._orig_connect_ex  # type: ignore[method-assign,assignment]
        self._installed = False

    def reset_attempts(self) -> None:
        with self._lock:
            self.attempts.clear()

    @contextmanager
    def active(self) -> Iterator[TransportDenialHarness]:
        """Context manager: install for the block, uninstall after."""
        was_installed = self._installed
        if not was_installed:
            self.install()
        try:
            yield self
        finally:
            if not was_installed:
                self.uninstall()


# ── Fake clock ─────────────────────────────────────────────────────────────────


class FakeClock:
    """
    Controllable clock utility. Not currently wired into RunContext or
    db.py (neither accepts an injected clock — RunContext._utc_now() and
    db.py's datetime('now') SQL defaults call real time directly), so this
    is standalone scaffolding a test can use for its own time-dependent
    assertions (e.g. a future P0-09 cancellation-within-30-seconds test),
    not something that currently changes musaeus's own behaviour.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def utcnow_iso(self) -> str:
        return self._now.isoformat(timespec="seconds")

    def advance(self, seconds: float = 0, minutes: float = 0, hours: float = 0) -> datetime:
        self._now += timedelta(seconds=seconds, minutes=minutes, hours=hours)
        return self._now

    def freeze(self, dt: datetime) -> None:
        self._now = dt


# ── Disposable vault ───────────────────────────────────────────────────────────


@dataclass
class DisposableVault:
    """
    Bundle of everything a test needs to exercise musaeus code against an
    isolated, disposable filesystem tree. Every path here lives under a
    single pytest tmp_path — nothing points outside it.
    """

    root: Path
    cfg: MusicConfig
    config_home: Path  # stands in for a redirected HOME/XDG base, per-vault
    recovery_root: Path  # disposable target for future P0-06+ recovery work
    report_root: Path  # disposable target for future P0-16+ reporting work

    def open_db(self) -> sqlite3.Connection:
        return open_db(self.cfg.db_path)

    def new_context(self, conn: sqlite3.Connection, dry_run: bool = False) -> RunContext:
        return RunContext.new(self.cfg, conn, dry_run=dry_run)


def make_disposable_vault(tmp_path: Path, name: str = "vault") -> DisposableVault:
    """Build a DisposableVault rooted entirely under *tmp_path*."""
    root = tmp_path / name
    cfg = MusicConfig(
        vault_root=root,
        inbox=root / "INBOX",
        staging=root / "STAGING",
        quarantine=root / "QUARANTINE",
        runs_root=root / "RUNS",
        meta_dir=root / "MetaData",
        alac_library=root / "ALAC-Library",
        db_path=root / "musaeus.db",
    )
    config_home = tmp_path / "fake_home"
    recovery_root = tmp_path / "fake_recovery"
    report_root = tmp_path / "fake_reports"
    for d in (config_home, recovery_root, report_root):
        d.mkdir(parents=True, exist_ok=True)
    return DisposableVault(
        root=root,
        cfg=cfg,
        config_home=config_home,
        recovery_root=recovery_root,
        report_root=report_root,
    )


# ── Before/after state snapshot ───────────────────────────────────────────────


@dataclass
class VaultSnapshot:
    """
    A point-in-time capture of a disposable vault's observable state, for
    before/after equality comparisons (MCR-001's zero-side-effect-preview
    acceptance criterion: "before/after comparison ... is identical").
    """

    directory_tree: tuple[str, ...]
    file_hashes: dict[str, str]  # relative path -> sha256 hex digest
    file_tags: dict[str, dict[str, str] | None]  # relative path -> best-effort tag dict
    db_exists: bool
    db_event_count: int
    db_archive_count: int
    db_content_checksum: str | None  # sha256 of a stable iterdump(), or None if no DB

    def diff(self, other: VaultSnapshot) -> list[str]:
        """Return a human-readable list of differences (empty = identical)."""
        diffs: list[str] = []
        if self.directory_tree != other.directory_tree:
            added = set(other.directory_tree) - set(self.directory_tree)
            removed = set(self.directory_tree) - set(other.directory_tree)
            if added:
                diffs.append(f"directories/files added: {sorted(added)}")
            if removed:
                diffs.append(f"directories/files removed: {sorted(removed)}")
        if self.file_hashes != other.file_hashes:
            diffs.append("file content hashes changed")
        if self.file_tags != other.file_tags:
            diffs.append("file tags changed")
        if self.db_exists != other.db_exists:
            diffs.append(f"db existence changed: {self.db_exists} -> {other.db_exists}")
        if self.db_event_count != other.db_event_count:
            diffs.append(f"db event count changed: {self.db_event_count} -> {other.db_event_count}")
        if self.db_archive_count != other.db_archive_count:
            diffs.append(
                f"db archive row count changed: {self.db_archive_count} -> {other.db_archive_count}"
            )
        if self.db_content_checksum != other.db_content_checksum:
            diffs.append("db content checksum changed")
        return diffs

    def equals(self, other: VaultSnapshot) -> bool:
        return not self.diff(other)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _best_effort_tags(path: Path) -> dict[str, str] | None:
    """Best-effort tag read via mutagen; returns None for non-audio/unparseable
    files (e.g. the placeholder b"FAKE FLAC DATA" bytes many tests write)."""
    try:
        import mutagen  # type: ignore[import-untyped]

        audio = mutagen.File(str(path))
        if audio is None or audio.tags is None:
            return None
        return {str(k): str(v) for k, v in audio.tags.items()}
    except Exception:
        return None


def _read_only_sqlite_uri(db_path: Path) -> str:
    """Build a read-only SQLite URI safe for paths containing ?/#/% ."""
    return f"file:{quote(str(db_path))}?mode=ro"


def _db_content_checksum(db_path: Path) -> str | None:
    """SHA-256 of a stable iterdump() text, or None if the DB doesn't exist.

    Deliberately not a raw-byte hash of the .db file: WAL checkpointing,
    VACUUM, and page-layout churn can all change the file's bytes without
    changing its logical content, which would make a raw-byte comparison
    report a false difference for a truly no-op operation.
    """
    if not db_path.exists():
        return None
    conn = sqlite3.connect(_read_only_sqlite_uri(db_path), uri=True)
    try:
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    return hashlib.sha256(dump.encode("utf-8")).hexdigest()


def snapshot_vault_state(vault: DisposableVault) -> VaultSnapshot:
    """Capture directory tree, file hashes/tags, and DB state for *vault*."""
    root = vault.root
    tree: list[str] = []
    hashes: dict[str, str] = {}
    tags: dict[str, dict[str, str] | None] = {}

    if root.exists():
        for p in sorted(root.rglob("*")):
            rel = str(p.relative_to(root))
            tree.append(rel + ("/" if p.is_dir() else ""))
            if p.is_file():
                hashes[rel] = _sha256_file(p)
                tags[rel] = _best_effort_tags(p)

    db_path = vault.cfg.db_path
    db_exists = db_path.exists()
    event_count = 0
    archive_count = 0
    if db_exists:
        conn = sqlite3.connect(_read_only_sqlite_uri(db_path), uri=True)
        try:
            conn.row_factory = sqlite3.Row
            try:
                event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            except sqlite3.OperationalError:
                event_count = 0
            try:
                archive_count = conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
            except sqlite3.OperationalError:
                archive_count = 0
        finally:
            conn.close()

    checksum = _db_content_checksum(db_path)

    return VaultSnapshot(
        directory_tree=tuple(tree),
        file_hashes=hashes,
        file_tags=tags,
        db_exists=db_exists,
        db_event_count=event_count,
        db_archive_count=archive_count,
        db_content_checksum=checksum,
    )
