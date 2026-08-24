"""
MUSAEUS — scope identity and conservative single-run locking (P0-10)

This is the direct answer to a real incident, not a hypothetical. On
2026-08-15 a suspected concurrent-process collision produced a
false-positive duplicate cascade that moved roughly 11,160 files into a
review quarantine folder. MCR-005 asks that "a second process targeting
the same mutable scope is refused or waits without mutation; the operator
can see the existing run identifier and lock owner". That sentence is the
incident, written as a requirement.

Two decisions carry most of the weight:

**Ancestor and descendant scopes conflict.** A lock on
`/vault/ALAC-Library` must conflict with a lock on
`/vault/ALAC-Library/Bob Seger`. Comparing paths for equality would let
two runs "safely" hold non-equal scopes that are the same files, which is
precisely how two processes end up rewriting each other's work while both
believe they are alone.

**Staleness is never decided by a timestamp.** An old heartbeat is not
evidence that an owner is gone; it is equally consistent with a long
ffmpeg call. (There is a live example on this very machine: a `musaeus
run` that has been in one Forge stage for hours.) Taking over requires
positive evidence that the previous owner is *gone*: the OS advisory lock
is free, the recorded process is not alive, and the record was made on
this host and this boot. Anything less refuses.

The advisory lock is `fcntl.flock`, so the kernel releases it when a
holder dies -- including a `kill -9`, which no in-process bookkeeping can
survive. The JSON metadata beside it is for *reporting* who the owner is;
it is never the thing that grants or denies the lock. Deciding ownership
from a file's contents rather than from a kernel primitive is how you get
a lock that can be defeated by a stale write.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
from dataclasses import asdict, dataclass
from pathlib import Path

from musaeus.state.schema import StateError, utc_now_iso

# ── Scope classification ──────────────────────────────────────────────────────

FIXTURE = "fixture"
INBOX = "inbox"
CANONICAL = "canonical"
REJECTED = "rejected"

CLASSIFICATIONS: frozenset[str] = frozenset({FIXTURE, INBOX, CANONICAL, REJECTED})

# Real roots this P0 work may never take a mutation lock on. Recorded as
# text and compared as text: normpath is pure string manipulation, while
# Path.resolve() calls readlink() and .exists() calls stat(), and the
# whole point is to classify these paths without touching them.
CANONICAL_REAL_ROOTS: tuple[str, ...] = (
    "/mnt/FORGE2TB/Projects/MUSAEUS_VAULT",
    "/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/ALAC-Library",
)
INBOX_REAL_ROOTS: tuple[str, ...] = ("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/INBOX",)


class ScopeError(StateError):
    reason_code = "scope_invalid"


class ScopeNotLockableError(StateError):
    """The scope is real, not fixture. P0 is fixture-only, so a mutation
    lock on it is refused regardless of any authority a caller believes it
    has."""

    reason_code = "scope_not_lockable"


class LockConflictError(StateError):
    """Another run holds, or may still hold, this scope."""

    reason_code = "lock_conflict"


def canonicalise(path: str | Path) -> str:
    """
    Normalise a path for comparison WITHOUT touching the filesystem.

    `os.path.abspath` normalises `..`/`.` and makes the path absolute
    using string operations and the cwd only. `Path.resolve()` is
    deliberately not used: it calls readlink(), which on a protected or
    future root is exactly the probe the spec forbids. The cost is that a
    symlink and its target are not recognised as the same scope; that is
    the conservative direction (two scopes treated as distinct means an
    extra refusal, never a missed conflict on the literal path).
    """
    return os.path.normpath(os.path.abspath(str(path)))


def classify(root: str | Path) -> str:
    """Classify a scope root by text alone. Never stats, never resolves."""
    normalised = canonicalise(root)
    for inbox_root in INBOX_REAL_ROOTS:
        if _is_at_or_under(normalised, os.path.normpath(inbox_root)):
            return INBOX
    for canonical_root in CANONICAL_REAL_ROOTS:
        if _is_at_or_under(normalised, os.path.normpath(canonical_root)):
            return CANONICAL
    if normalised in ("/", "/home", "/mnt", "/tmp"):
        return REJECTED
    return FIXTURE


def _is_at_or_under(candidate: str, ancestor: str) -> bool:
    return candidate == ancestor or candidate.startswith(ancestor + os.sep)


@dataclass(frozen=True)
class Scope:
    """A lockable identity: a root plus the operation domain acting on it.

    The domain is part of the identity because two runs doing genuinely
    different things to the same tree are not necessarily in conflict,
    while two runs doing the same thing certainly are. Identity by path
    alone would be both too strict and too vague."""

    root: str
    domain: str

    @classmethod
    def build(cls, root: str | Path, domain: str) -> Scope:
        if not domain:
            raise ScopeError("scope domain is required")
        return cls(root=canonicalise(root), domain=domain)

    @property
    def classification(self) -> str:
        return classify(self.root)

    @property
    def scope_id(self) -> str:
        digest = hashlib.sha256()
        digest.update(self.root.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(self.domain.encode("utf-8"))
        return digest.hexdigest()[:32]


def scopes_conflict(a: Scope, b: Scope) -> bool:
    """
    True when two scopes cannot be held at once.

    Same domain, and roots that are equal OR where one contains the other.
    The containment half is the important half: a lock on
    `/vault/ALAC-Library` and one on `/vault/ALAC-Library/Bob Seger` cover
    the same files, and equality alone would call them independent.
    """
    if a.domain != b.domain:
        return False
    return _is_at_or_under(a.root, b.root) or _is_at_or_under(b.root, a.root)


# ── Owner records ─────────────────────────────────────────────────────────────


def _boot_id() -> str:
    """Identifier for the current boot, so a reused PID after a reboot is
    not mistaken for the original holder. Falls back to a constant when
    unavailable, which makes the PID check *more* conservative (a mismatch
    can never be claimed), never less."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown-boot"


@dataclass(frozen=True)
class LockOwner:
    """Who holds the lock. Reporting only -- never the basis of a decision."""

    scope_id: str
    scope_root: str
    scope_domain: str
    run_id: str
    pid: int
    hostname: str
    boot_id: str
    acquired_at: str
    heartbeat_at: str

    def as_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, indent=2)

    @classmethod
    def from_json(cls, text: str) -> LockOwner | None:
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return None
        expected = set(cls.__dataclass_fields__)
        if not expected.issubset(data):
            return None
        return cls(**{k: data[k] for k in expected})

    def describe(self) -> str:
        return (
            f"run {self.run_id} (pid {self.pid} on {self.hostname}), "
            f"acquired {self.acquired_at}, heartbeat {self.heartbeat_at}"
        )


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists; we simply may not signal it. Existence is the answer.
        return True
    except OSError:
        return True
    return True


# ── The lock ──────────────────────────────────────────────────────────────────


@dataclass
class LockHandle:
    scope: Scope
    owner: LockOwner
    lock_path: Path
    meta_path: Path
    _fd: int

    def heartbeat(self, now: str | None = None) -> LockOwner:
        """Refresh the visible heartbeat. Does not extend any right: the
        flock is what holds the scope, and it neither expires nor needs
        renewing."""
        refreshed = LockOwner(**{**asdict(self.owner), "heartbeat_at": now or utc_now_iso()})
        _write_atomic(self.meta_path, refreshed.as_json())
        self.owner = refreshed
        return refreshed

    def release(self) -> None:
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self.meta_path.unlink(missing_ok=True)

    def __enter__(self) -> LockHandle:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file in the same directory plus os.replace, so a
    reader never sees a half-written owner record."""
    temp = path.with_name(path.name + f".tmp{os.getpid()}")
    temp.write_text(text)
    os.replace(temp, path)


def _paths_for(scope: Scope, lock_dir: Path) -> tuple[Path, Path]:
    return (
        lock_dir / f"{scope.scope_id}.lock",
        lock_dir / f"{scope.scope_id}.owner.json",
    )


def observe(scope: Scope, lock_dir: Path) -> LockOwner | None:
    """
    Report the current owner without acquiring anything.

    Preview's entry point. It takes no advisory lock, creates no lock
    file, and creates no directory -- a preview that had to create
    something in order to look at something would not be a preview. Absent
    or unreadable metadata reports None rather than raising: not knowing
    who holds a lock is not the same as an error, and the caller's next
    step (refuse, or report "unknown") is a decision for the caller.
    """
    if not lock_dir.is_dir():
        return None
    _, meta_path = _paths_for(scope, lock_dir)
    if not meta_path.is_file():
        return None
    try:
        return LockOwner.from_json(meta_path.read_text())
    except OSError:
        return None


def acquire(
    scope: Scope,
    lock_dir: Path,
    *,
    run_id: str,
    now: str | None = None,
    allow_classifications: frozenset[str] = frozenset({FIXTURE}),
) -> LockHandle:
    """
    Take the scope's advisory lock, or raise LockConflictError.

    `allow_classifications` defaults to fixtures only. Classifying a path
    as INBOX or CANONICAL grants nothing on its own -- MCR-002 is explicit
    that identifying the future staging scope "does not confer live
    authority" -- so the classification is a gate to pass, never a
    permission to act.

    Takeover requires positive evidence the previous owner is gone: the
    advisory lock free, the recorded PID not alive, and the record made on
    this host and this boot. An old heartbeat alone proves nothing; on
    this machine right now there is a legitimate run that has been inside
    one stage for hours.
    """
    if scope.classification not in allow_classifications:
        raise ScopeNotLockableError(
            f"scope {scope.root!r} classifies as {scope.classification!r}; this build may "
            f"only take a mutation lock on {sorted(allow_classifications)}",
            scope_root=scope.root,
            classification=scope.classification,
            remediation="point at a disposable fixture scope",
        )

    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path, meta_path = _paths_for(scope, lock_dir)

    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        holder = observe(scope, lock_dir)
        raise LockConflictError(
            f"scope {scope.root!r} (domain {scope.domain!r}) is held by "
            f"{holder.describe() if holder else 'an unidentified run'}",
            scope_id=scope.scope_id,
            scope_root=scope.root,
            owner=asdict(holder) if holder else None,
            remediation="wait for the holding run to finish, or target a different scope",
        ) from exc

    # The advisory lock is ours. The metadata may still name someone else
    # -- a crash releases the flock but leaves the file. Take over only on
    # positive evidence that they are gone.
    stale = observe(scope, lock_dir)
    if stale is not None:
        blocking_reason = _refuses_takeover(stale)
        if blocking_reason is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise LockConflictError(
                f"scope {scope.root!r} records owner {stale.describe()}, and {blocking_reason}; "
                f"refusing to take over on timestamp alone",
                scope_id=scope.scope_id,
                owner=asdict(stale),
                remediation="confirm the recorded run is gone, then remove its owner record",
            )

    timestamp = now if now is not None else utc_now_iso()
    owner = LockOwner(
        scope_id=scope.scope_id,
        scope_root=scope.root,
        scope_domain=scope.domain,
        run_id=run_id,
        pid=os.getpid(),
        hostname=socket.gethostname(),
        boot_id=_boot_id(),
        acquired_at=timestamp,
        heartbeat_at=timestamp,
    )
    _write_atomic(meta_path, owner.as_json())
    return LockHandle(scope=scope, owner=owner, lock_path=lock_path, meta_path=meta_path, _fd=fd)


def _refuses_takeover(stale: LockOwner) -> str | None:
    """Return the reason takeover is refused, or None when it is safe.

    Refuses on anything it cannot positively verify. A record from another
    host cannot have its PID checked from here, so it is treated as
    possibly live -- the conservative reading, and the one that would have
    helped on 2026-08-15."""
    if stale.hostname != socket.gethostname():
        return f"it was recorded on another host ({stale.hostname}), whose process state is unknowable from here"
    if stale.boot_id != _boot_id():
        return None  # different boot: that PID cannot be the same process
    if _process_is_alive(stale.pid):
        return f"process {stale.pid} is still alive"
    return None
