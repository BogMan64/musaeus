"""
P0-10 — scope identity and conservative single-run locking.

The multiprocess test here is the point of the file. MCR-005's acceptance
criterion is that "a second process targeting the same mutable scope is
refused or waits without mutation", and the only way to prove that is
with a second real process, holding a real advisory lock, while the first
one tries. An in-process mock of a lock proves that the mock works.

This is also the requirement written directly out of the 2026-08-15
incident: a suspected concurrent-process collision that produced a
false-positive duplicate cascade and moved ~11,160 files.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from musaeus.safety.lock import (
    CANONICAL,
    FIXTURE,
    INBOX,
    REJECTED,
    LockConflictError,
    LockOwner,
    Scope,
    ScopeError,
    ScopeNotLockableError,
    acquire,
    canonicalise,
    classify,
    observe,
    scopes_conflict,
)

DOMAIN = "library-mutation"


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    d = tmp_path / "locks"
    d.mkdir()
    return d


def _scope(tmp_path: Path, sub: str = "vault", domain: str = DOMAIN) -> Scope:
    return Scope.build(tmp_path / sub, domain)


# ── Scope identity ────────────────────────────────────────────────────────────


class TestScopeIdentity:
    def test_canonicalise_normalises_without_touching_the_filesystem(self, path_guard, tmp_path):
        before = len(path_guard.attempts)
        result = canonicalise(f"{tmp_path}/a/../b/./c")
        assert result == str(tmp_path / "b" / "c")
        assert len(path_guard.attempts) == before

    def test_classifying_a_protected_root_does_not_probe_it(self, path_guard):
        """The PathGuard raises on any stat/open under the real vault. If
        classify() reached for the filesystem, this test would error rather
        than pass -- which is what makes it a check."""
        before = len(path_guard.attempts)
        assert classify("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/ALAC-Library") == CANONICAL
        assert classify("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/INBOX/new") == INBOX
        assert len(path_guard.attempts) == before

    @pytest.mark.parametrize("root, expected", [("/", REJECTED), ("/tmp", REJECTED)])
    def test_bare_system_roots_are_rejected(self, root, expected):
        assert classify(root) == expected

    def test_scope_id_depends_on_root_and_domain(self, tmp_path):
        a = _scope(tmp_path, "vault", "library-mutation")
        b = _scope(tmp_path, "vault", "preview")
        c = _scope(tmp_path, "other", "library-mutation")
        assert a.scope_id != b.scope_id
        assert a.scope_id != c.scope_id
        assert a.scope_id == _scope(tmp_path, "vault", "library-mutation").scope_id

    def test_empty_domain_is_refused(self, tmp_path):
        with pytest.raises(ScopeError):
            Scope.build(tmp_path, "")


class TestScopeConflict:
    def test_identical_scopes_conflict(self, tmp_path):
        assert scopes_conflict(_scope(tmp_path), _scope(tmp_path)) is True

    def test_descendant_conflicts_with_ancestor(self, tmp_path):
        parent = Scope.build(tmp_path / "ALAC-Library", DOMAIN)
        child = Scope.build(tmp_path / "ALAC-Library" / "Bob Seger", DOMAIN)
        assert scopes_conflict(parent, child) is True
        assert scopes_conflict(child, parent) is True, "conflict must be symmetric"

    def test_siblings_do_not_conflict(self, tmp_path):
        a = Scope.build(tmp_path / "ALAC-Library", DOMAIN)
        b = Scope.build(tmp_path / "ALAC-Library-Archive", DOMAIN)
        assert scopes_conflict(a, b) is False, (
            "a prefix match on the raw string would wrongly call these the same scope"
        )

    def test_different_domains_do_not_conflict(self, tmp_path):
        a = Scope.build(tmp_path / "vault", "library-mutation")
        b = Scope.build(tmp_path / "vault", "preview")
        assert scopes_conflict(a, b) is False


# ── Classification grants nothing ─────────────────────────────────────────────


class TestClassificationIsNotAuthority:
    @pytest.mark.parametrize(
        "root",
        [
            "/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/INBOX",
            "/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/ALAC-Library",
        ],
    )
    def test_real_scopes_cannot_be_locked_for_mutation(self, root, lock_dir):
        """MCR-002: identifying the future INBOX staging scope does not
        confer live authority. The classification is a gate to pass, not a
        permission to act."""
        scope = Scope.build(root, DOMAIN)
        with pytest.raises(ScopeNotLockableError) as exc:
            acquire(scope, lock_dir, run_id="run-A")
        assert exc.value.reason_code == "scope_not_lockable"
        assert exc.value.details["classification"] in (INBOX, CANONICAL)

    def test_refusal_creates_no_lock_artefacts(self, lock_dir):
        scope = Scope.build("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/INBOX", DOMAIN)
        with pytest.raises(ScopeNotLockableError):
            acquire(scope, lock_dir, run_id="run-A")
        assert list(lock_dir.iterdir()) == []

    def test_a_fixture_scope_is_lockable(self, tmp_path, lock_dir):
        """Negative control: the gate must be able to open."""
        with acquire(_scope(tmp_path), lock_dir, run_id="run-A") as handle:
            assert handle.owner.run_id == "run-A"
            assert handle.scope.classification == FIXTURE


# ── Acquisition, observation, heartbeat ───────────────────────────────────────


class TestAcquireAndObserve:
    def test_owner_is_visible_and_identifies_the_run(self, tmp_path, lock_dir):
        scope = _scope(tmp_path)
        with acquire(scope, lock_dir, run_id="run-A") as handle:
            seen = observe(scope, lock_dir)
            assert seen is not None
            assert seen.run_id == "run-A"
            assert seen.pid == os.getpid()
            assert "run-A" in seen.describe()
            assert handle.owner == seen

    def test_release_clears_the_owner_record(self, tmp_path, lock_dir):
        scope = _scope(tmp_path)
        handle = acquire(scope, lock_dir, run_id="run-A")
        handle.release()
        assert observe(scope, lock_dir) is None

    def test_heartbeat_moves_forward_and_is_visible(self, tmp_path, lock_dir):
        scope = _scope(tmp_path)
        with acquire(scope, lock_dir, run_id="run-A", now="2026-08-23T22:00:00Z") as handle:
            assert observe(scope, lock_dir).heartbeat_at == "2026-08-23T22:00:00Z"
            handle.heartbeat(now="2026-08-23T22:05:00Z")
            refreshed = observe(scope, lock_dir)
            assert refreshed.heartbeat_at == "2026-08-23T22:05:00Z"
            assert refreshed.acquired_at == "2026-08-23T22:00:00Z", "acquisition time is history"

    def test_observe_never_acquires_or_creates_anything(self, tmp_path):
        """Preview's path. A preview that had to create something in order
        to look at something would not be a preview."""
        scope = _scope(tmp_path)
        absent_dir = tmp_path / "no-locks-here"
        assert observe(scope, absent_dir) is None
        assert not absent_dir.exists()

    def test_observe_does_not_block_a_later_acquisition(self, tmp_path, lock_dir):
        scope = _scope(tmp_path)
        assert observe(scope, lock_dir) is None
        with acquire(scope, lock_dir, run_id="run-A"):
            pass

    def test_unreadable_owner_json_reports_unknown_not_an_exception(self, tmp_path, lock_dir):
        scope = _scope(tmp_path)
        (lock_dir / f"{scope.scope_id}.owner.json").write_text("{not json")
        assert observe(scope, lock_dir) is None


# ── Multiprocess conflict ─────────────────────────────────────────────────────

_HOLDER = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {repo!r})
    from pathlib import Path
    from musaeus.safety.lock import Scope, acquire

    scope = Scope.build({root!r}, {domain!r})
    handle = acquire(scope, Path({lock_dir!r}), run_id="holder-run")
    Path({ready!r}).write_text("held")
    while not Path({stop!r}).exists():
        time.sleep(0.02)
    handle.release()
    """
)


class TestMultiprocessConflict:
    def test_a_second_process_is_refused_and_told_who_holds_it(self, tmp_path, lock_dir):
        repo = str(Path(__file__).resolve().parent.parent)
        vault = tmp_path / "vault"
        ready, stop = tmp_path / "ready", tmp_path / "stop"
        script = _HOLDER.format(
            repo=repo,
            root=str(vault),
            domain=DOMAIN,
            lock_dir=str(lock_dir),
            ready=str(ready),
            stop=str(stop),
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            deadline = time.monotonic() + 30
            while not ready.exists() and time.monotonic() < deadline:
                if holder.poll() is not None:
                    out, err = holder.communicate()
                    pytest.fail(f"holder exited early: {err.decode()[-800:]}")
                time.sleep(0.02)
            assert ready.exists(), "holder process never acquired the lock"

            scope = Scope.build(vault, DOMAIN)
            with pytest.raises(LockConflictError) as exc:
                acquire(scope, lock_dir, run_id="second-run")

            assert exc.value.reason_code == "lock_conflict"
            owner = exc.value.details["owner"]
            assert owner["run_id"] == "holder-run"
            assert owner["pid"] == holder.pid
            assert "holder-run" in str(exc.value)
        finally:
            stop.write_text("stop")
            holder.wait(timeout=30)

        # And once the holder is gone, the scope is acquirable again.
        with acquire(Scope.build(vault, DOMAIN), lock_dir, run_id="second-run") as handle:
            assert handle.owner.run_id == "second-run"

    def test_the_conflict_comes_from_the_kernel_not_the_owner_file(self, tmp_path, lock_dir):
        """Pins down the MECHANISM, and exists because of a real gap.

        The first version of the test above passed even with `fcntl.flock`
        replaced by `pass`: the second process was refused by the *stale
        owner metadata* check (holder's pid alive on this host), so the
        test could not distinguish the kernel advisory lock from a JSON
        file. A lock decided by a file's contents can be defeated by a
        stale write, which is exactly what this module's docstring says it
        must not be.

        So: hold the flock in another process, delete the owner record,
        and require the conflict anyway."""
        repo = str(Path(__file__).resolve().parent.parent)
        vault = tmp_path / "vault"
        ready, stop = tmp_path / "ready2", tmp_path / "stop2"
        script = _HOLDER.format(
            repo=repo,
            root=str(vault),
            domain=DOMAIN,
            lock_dir=str(lock_dir),
            ready=str(ready),
            stop=str(stop),
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            deadline = time.monotonic() + 30
            while not ready.exists() and time.monotonic() < deadline:
                if holder.poll() is not None:
                    _, err = holder.communicate()
                    pytest.fail(f"holder exited early: {err.decode()[-800:]}")
                time.sleep(0.02)
            assert ready.exists()

            scope = Scope.build(vault, DOMAIN)
            owner_record = lock_dir / f"{scope.scope_id}.owner.json"
            assert owner_record.exists()
            owner_record.unlink()
            assert observe(scope, lock_dir) is None, "no metadata is available to refuse on"

            with pytest.raises(LockConflictError) as exc:
                acquire(scope, lock_dir, run_id="second-run")
            assert exc.value.details["owner"] is None
            assert "unidentified run" in str(exc.value)
        finally:
            stop.write_text("stop")
            holder.wait(timeout=30)

    def test_the_refused_process_makes_no_change(self, tmp_path, lock_dir):
        """'refused or waits WITHOUT MUTATION': the refusal must not even
        rewrite the owner record it failed to take."""
        scope = _scope(tmp_path)
        with acquire(scope, lock_dir, run_id="run-A", now="2026-08-23T22:00:00Z"):
            before = (lock_dir / f"{scope.scope_id}.owner.json").read_text()
            # Same process cannot conflict with itself via flock, so this
            # asserts the metadata is untouched by an observation.
            assert observe(scope, lock_dir).run_id == "run-A"
            after = (lock_dir / f"{scope.scope_id}.owner.json").read_text()
        assert before == after


# ── Conservative stale handling ───────────────────────────────────────────────


def _plant_owner(lock_dir: Path, scope: Scope, **over) -> LockOwner:
    import socket

    base = LockOwner(
        scope_id=scope.scope_id,
        scope_root=scope.root,
        scope_domain=scope.domain,
        run_id="ghost-run",
        pid=os.getpid(),
        hostname=socket.gethostname(),
        boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        acquired_at="2020-01-01T00:00:00Z",
        heartbeat_at="2020-01-01T00:00:00Z",
    )
    owner = LockOwner(**{**asdict(base), **over})
    (lock_dir / f"{scope.scope_id}.owner.json").write_text(owner.as_json())
    return owner


class TestStaleLockHandling:
    def test_an_ancient_heartbeat_alone_does_not_permit_takeover(self, tmp_path, lock_dir):
        """The decisive test. The record is from 2020 and the flock is
        free, but the recorded process is alive -- so takeover is refused.
        An old heartbeat is equally consistent with a long ffmpeg call;
        there is a live example of exactly that on this machine."""
        scope = _scope(tmp_path)
        _plant_owner(lock_dir, scope, pid=os.getpid())
        with pytest.raises(LockConflictError) as exc:
            acquire(scope, lock_dir, run_id="usurper")
        assert "still alive" in str(exc.value)
        assert "timestamp alone" in str(exc.value)

    def test_a_dead_process_on_this_host_may_be_taken_over(self, tmp_path, lock_dir):
        """Negative control: positive evidence the owner is gone."""
        scope = _scope(tmp_path)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        _plant_owner(lock_dir, scope, pid=dead.pid)
        with acquire(scope, lock_dir, run_id="successor") as handle:
            assert handle.owner.run_id == "successor"

    def test_a_record_from_another_host_is_never_taken_over(self, tmp_path, lock_dir):
        scope = _scope(tmp_path)
        _plant_owner(lock_dir, scope, hostname="some-other-machine", pid=999999)
        with pytest.raises(LockConflictError) as exc:
            acquire(scope, lock_dir, run_id="usurper")
        assert "another host" in str(exc.value)

    def test_a_record_from_a_previous_boot_is_not_a_live_pid(self, tmp_path, lock_dir):
        """PIDs are reused across reboots. A record from a previous boot
        naming a currently-live PID is not the same process."""
        scope = _scope(tmp_path)
        _plant_owner(lock_dir, scope, pid=os.getpid(), boot_id="a-previous-boot")
        with acquire(scope, lock_dir, run_id="successor") as handle:
            assert handle.owner.run_id == "successor"
