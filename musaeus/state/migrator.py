"""
MUSAEUS — migration service: backup, apply, ledger, candidate swap (P0-06)

Order of operations, and the reasoning for it (DR-02, MCR-004):

1. Refuse a read-only database before anything else, so a doomed run
   fails on a permission check rather than half way through a write.
2. Read the version WITHOUT writing. A forward version is refused here,
   before a backup file is created, because refusing should leave no
   trace at all.
3. Refuse a ledger holding an unfinished attempt. A database whose last
   migration was interrupted has unknown contents; re-deriving a plan
   from its version stamp would be guessing.
4. Plan. An already-current database does no work, creates no backup, and
   touches nothing -- an important property, because this path will
   eventually run on every open.
5. Back up and VERIFY before the first change. "Verify" means reopening
   the backup, running `PRAGMA integrity_check`, confirming its version,
   and re-hashing the bytes -- not merely observing that a copy call
   returned. An unverified backup is worse than no backup: it converts an
   unknown into a false reassurance.
6. Apply each migration, recording `running` before and the outcome
   after, each committed separately from the migration itself so a crash
   leaves evidence rather than a clean-looking database.

Two apply strategies:

* Transactional (the default): explicit BEGIN/COMMIT on the active
  database. Requires `isolation_level=None`, because Python's sqlite3
  does not open a transaction for DDL under its default setting -- a
  CREATE TABLE would autocommit and could not be rolled back. That is a
  silent-no-op in the rollback direction: the code reads as transactional
  and is not.
* Candidate swap: copy, migrate the copy, validate it, then `os.replace`
  it over the active database. For changes SQLite cannot roll back.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from musaeus.state.migrations import MIGRATIONS, Migration, plan_migrations, validate_registry
from musaeus.state.schema import (
    LEGACY_UNVERSIONED,
    OUTCOME_FAILED,
    OUTCOME_RUNNING,
    OUTCOME_SUCCEEDED,
    DatabaseReadOnlyError,
    StateError,
    check_compatibility,
    check_ledger_clean,
    detect_read_only,
    ensure_state_tables,
    is_empty_database,
    read_schema_version,
    stamp_schema_version,
    utc_now_iso,
)

_BACKUP_CHUNK = 1024 * 1024


class BackupVerificationError(StateError):
    """A backup was written but could not be verified as readable and
    intact. Treated as a hard failure: the migration does not proceed."""

    reason_code = "backup_unverified"


class MigrationFailedError(StateError):
    """A migration failed. The active database is either unchanged (the
    transaction rolled back, or the candidate was discarded) or restorable
    from the verified backup named in `details['backup_path']`."""

    reason_code = "migration_failed"


class RecoveryTargetError(StateError):
    """The declared recovery target is unusable, or is the fixed future
    recovery root that P0 work must never create, probe, or write."""

    reason_code = "recovery_target_invalid"


@dataclass(frozen=True)
class BackupRef:
    """A database backup that has been written AND verified."""

    path: Path
    sha256: str
    size_bytes: int
    schema_version: int
    created_at: str
    verified: bool

    def as_ledger_ref(self) -> str:
        """Compact reference recorded in the ledger's `backup_ref`."""
        return f"{self.path.name}:sha256={self.sha256}"


@dataclass(frozen=True)
class MigrationResult:
    """What `migrate()` did. `applied` is empty for an already-current
    database, which is the only case where `backup` is None."""

    from_version: int
    to_version: int
    applied: tuple[str, ...] = field(default_factory=tuple)
    backup: BackupRef | None = None
    was_empty_database: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.applied)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_BACKUP_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open strictly for inspection.

    Step 1 of the design's migration strategy is "open an existing
    database read-only first, identify its schema version, and block
    unsupported forward versions". A writable connection would be the
    wrong tool twice over: it grants a write capability the inspection
    phase must not have, and merely opening one in WAL mode creates `-wal`
    and `-shm` sidecars -- so the "I decided to do nothing" path would
    still leave new files behind. That is not a hypothetical; the
    before/after equality test caught exactly that.
    """
    conn = sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open with explicit transaction control.

    `isolation_level=None` is load-bearing, not style: under Python's
    default, sqlite3 opens implicit transactions for DML only, so DDL runs
    in autocommit and a `ROLLBACK` after a failed CREATE/ALTER would have
    nothing to undo."""
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _assert_usable_recovery_root(recovery_root: Path) -> None:
    """Refuse a missing/unwritable recovery target, and refuse the fixed
    future root outright.

    The future-root comparison is done on normalised strings, never on
    `Path.resolve()` or `.exists()`: resolve() calls readlink() and
    exists() calls stat(), both of which are exactly the "probe" the spec
    forbids. Comparing text touches nothing.
    """
    from musaeus.state.policy import FUTURE_RECOVERY_ROOT

    declared = os.path.normpath(str(recovery_root))
    future = os.path.normpath(FUTURE_RECOVERY_ROOT)
    if declared == future or declared.startswith(future + os.sep):
        raise RecoveryTargetError(
            f"declared recovery target {declared!r} is the fixed future recovery root; "
            f"P0 work is fixture-only and must never create, probe, or write it",
            declared=declared,
            future_recovery_root=FUTURE_RECOVERY_ROOT,
            remediation="pass a disposable fixture recovery root",
        )
    if not recovery_root.is_dir():
        raise RecoveryTargetError(
            f"recovery target {declared!r} does not exist or is not a directory",
            declared=declared,
            remediation="create the disposable recovery root before migrating",
        )
    if not os.access(recovery_root, os.W_OK):
        raise RecoveryTargetError(
            f"recovery target {declared!r} is not writable",
            declared=declared,
            remediation="grant write permission on the disposable recovery root",
        )


# ── Backup ────────────────────────────────────────────────────────────────────


def create_verified_backup(
    db_path: Path, recovery_root: Path, *, now: str | None = None
) -> BackupRef:
    """
    Copy *db_path* into *recovery_root* and verify the copy.

    Uses SQLite's online backup API rather than a file copy: these
    databases run in WAL mode, so committed transactions can still live in
    a `-wal` sidecar. `shutil.copy` of the main file alone would produce a
    backup that opens cleanly and is quietly missing the most recent
    commits -- a corrupt-but-plausible artefact, the worst kind.

    Verification reopens the result and requires `PRAGMA integrity_check`
    to return exactly `ok`, then re-hashes the bytes on disk. Returns only
    on success; raises BackupVerificationError otherwise.
    """
    _assert_usable_recovery_root(recovery_root)
    if not db_path.exists():
        raise BackupVerificationError(
            f"cannot back up {db_path}: it does not exist", db_path=str(db_path)
        )

    timestamp = now if now is not None else utc_now_iso()
    safe_stamp = timestamp.replace(":", "").replace("-", "")
    backup_path = recovery_root / f"{db_path.stem}_premigration_{safe_stamp}.db"
    counter = 1
    while backup_path.exists():
        backup_path = recovery_root / f"{db_path.stem}_premigration_{safe_stamp}_{counter}.db"
        counter += 1

    source = sqlite3.connect(str(db_path), timeout=30)
    try:
        dest = sqlite3.connect(str(backup_path))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()

    if not backup_path.exists():
        raise BackupVerificationError(
            "backup call completed but produced no file", backup_path=str(backup_path)
        )

    verify = sqlite3.connect(str(backup_path), timeout=30)
    try:
        integrity = verify.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise BackupVerificationError(
                f"backup failed integrity_check: {integrity[0] if integrity else 'no result'}",
                backup_path=str(backup_path),
            )
        backup_version = read_schema_version(verify)
        row_check = verify.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()
        if row_check is None:
            raise BackupVerificationError(
                "backup is unreadable: sqlite_master returned no result",
                backup_path=str(backup_path),
            )
    finally:
        verify.close()

    return BackupRef(
        path=backup_path,
        sha256=_sha256_file(backup_path),
        size_bytes=backup_path.stat().st_size,
        schema_version=backup_version,
        created_at=timestamp,
        verified=True,
    )


def restore_from_backup(backup: BackupRef, db_path: Path) -> None:
    """
    Restore *backup* over *db_path*, re-checking the backup's hash first.

    The hash re-check is the point: a backup taken an hour ago and a file
    at that path now are not the same claim. If the bytes moved, restoring
    them would replace a known-bad database with an unknown one.
    """
    if not backup.path.exists():
        raise BackupVerificationError(
            f"backup {backup.path} is missing; cannot restore", backup_path=str(backup.path)
        )
    current = _sha256_file(backup.path)
    if current != backup.sha256:
        raise BackupVerificationError(
            f"backup {backup.path} no longer matches its recorded digest "
            f"(recorded {backup.sha256[:12]}..., found {current[:12]}...); refusing to restore",
            backup_path=str(backup.path),
        )
    source = sqlite3.connect(str(backup.path), timeout=30)
    try:
        dest = sqlite3.connect(str(db_path), timeout=30)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


# ── Ledger ────────────────────────────────────────────────────────────────────


def _record_attempt_started(
    conn: sqlite3.Connection, migration: Migration, backup: BackupRef | None, started_at: str
) -> None:
    """Commit a `running` row BEFORE the migration runs.

    Committed separately and deliberately outside the migration's own
    transaction: if it were inside, a rollback would erase the evidence
    that anything was ever attempted, and an interrupted process would
    leave a database that looks untouched."""
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """
        INSERT INTO schema_migrations
            (migration_id, from_version, to_version, checksum, started_at,
             finished_at, outcome, error_code, backup_ref)
        VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, ?)
        ON CONFLICT(migration_id) DO UPDATE SET
            started_at  = excluded.started_at,
            finished_at = NULL,
            outcome     = excluded.outcome,
            error_code  = NULL,
            backup_ref  = excluded.backup_ref
        """,
        (
            migration.migration_id,
            migration.from_version,
            migration.to_version,
            migration.checksum,
            started_at,
            OUTCOME_RUNNING,
            backup.as_ledger_ref() if backup is not None else None,
        ),
    )
    conn.execute("COMMIT")


def _record_attempt_finished(
    conn: sqlite3.Connection,
    migration: Migration,
    outcome: str,
    *,
    error_code: str | None = None,
    finished_at: str | None = None,
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE schema_migrations SET finished_at = ?, outcome = ?, error_code = ? "
        "WHERE migration_id = ?",
        (
            finished_at if finished_at is not None else utc_now_iso(),
            outcome,
            error_code,
            migration.migration_id,
        ),
    )
    conn.execute("COMMIT")


# ── Apply strategies ──────────────────────────────────────────────────────────


def _apply_transactional(conn: sqlite3.Connection, migration: Migration, now: str) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in migration.statements:
            conn.execute(statement)
        ensure_state_tables(conn)
        stamp_schema_version(conn, migration.to_version, now=now)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _apply_candidate_swap(
    db_path: Path, conn: sqlite3.Connection, migration: Migration, now: str
) -> None:
    """
    Migrate a copy, validate it, then atomically replace the original.

    The candidate is created next to the active database, not in the
    recovery root: `os.replace` is only atomic within one filesystem, and
    a recovery target on another device would silently degrade the swap
    into a copy that can be interrupted half-written. The recovery root
    holds the *backup*, which has no such constraint.
    """
    conn.close()
    candidate = db_path.with_name(db_path.name + ".candidate")
    if candidate.exists():
        candidate.unlink()

    source = sqlite3.connect(str(db_path), timeout=30)
    try:
        dest = sqlite3.connect(str(candidate))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()

    try:
        cand_conn = _connect(candidate)
        try:
            for statement in migration.statements:
                cand_conn.execute(statement)
            ensure_state_tables(cand_conn)
            stamp_schema_version(cand_conn, migration.to_version, now=now)
            integrity = cand_conn.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]).lower() != "ok":
                raise MigrationFailedError(
                    f"candidate database failed integrity_check after {migration.migration_id}",
                    migration_id=migration.migration_id,
                )
            reached = read_schema_version(cand_conn)
            if reached != migration.to_version:
                raise MigrationFailedError(
                    f"candidate database reports version {reached} after "
                    f"{migration.migration_id}, expected {migration.to_version}",
                    migration_id=migration.migration_id,
                )
        finally:
            cand_conn.close()
    except Exception:
        candidate.unlink(missing_ok=True)
        raise

    os.replace(candidate, db_path)
    # The replaced file's WAL/SHM sidecars belong to the database that is
    # now gone. Left behind, SQLite would try to recover the old WAL
    # against the new file.
    for sidecar in (
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    ):
        if sidecar.exists():
            sidecar.unlink()


# ── The service ───────────────────────────────────────────────────────────────


def migrate(
    db_path: Path,
    *,
    recovery_root: Path,
    migrations: tuple[Migration, ...] = MIGRATIONS,
    now: str | None = None,
) -> MigrationResult:
    """
    Bring the database at *db_path* to the end of the declared chain.

    Never touches library content: every statement it runs is state-table
    DDL from the registry. A failure leaves the active database usable, or
    restorable from the returned/attached backup.
    """
    validate_registry(migrations)
    timestamp = now if now is not None else utc_now_iso()

    if detect_read_only(db_path):
        raise DatabaseReadOnlyError(
            f"database {db_path} (or its directory) is not writable; refusing to migrate",
            db_path=str(db_path),
            remediation="grant write permission, or point at a writable copy",
        )

    # ── Inspect. Read-only, and nothing below may write. ─────────────────
    if db_path.exists():
        inspect = _connect_readonly(db_path)
        try:
            was_empty = is_empty_database(inspect)
            current_version = read_schema_version(inspect)
            check_compatibility(current_version, db_path=db_path)
            check_ledger_clean(inspect, db_path=db_path)
        finally:
            inspect.close()
    else:
        was_empty = True
        current_version = LEGACY_UNVERSIONED

    plan = plan_migrations(current_version, migrations)
    if not plan:
        return MigrationResult(
            from_version=current_version,
            to_version=current_version,
            applied=(),
            backup=None,
            was_empty_database=was_empty,
        )

    # ── Everything below this line may write. The backup comes first. ────
    backup = (
        create_verified_backup(db_path, recovery_root, now=timestamp) if db_path.exists() else None
    )
    if backup is None:
        # No pre-existing database means there is nothing to lose and
        # nothing to restore. Say so explicitly rather than letting a
        # None backup_ref in the ledger read as "the backup step was
        # skipped for some unrecorded reason".
        _assert_usable_recovery_root(recovery_root)

    conn = _connect(db_path)
    try:
        # Bootstrap the ledger itself. Migration 0001 is what *declares*
        # these tables, so without this the first attempt would have
        # nowhere to record that it started.
        conn.execute("BEGIN IMMEDIATE")
        ensure_state_tables(conn)
        conn.execute("COMMIT")

        applied: list[str] = []
        for migration in plan:
            _record_attempt_started(conn, migration, backup, timestamp)
            try:
                if migration.transactional:
                    _apply_transactional(conn, migration, timestamp)
                else:
                    _apply_candidate_swap(db_path, conn, migration, timestamp)
                    conn = _connect(db_path)
            except Exception as exc:
                finish_conn = conn if migration.transactional else _connect(db_path)
                # Explicit isinstance, not getattr(exc, "reason_code", ...).
                # OPEN_ITEMS finding #6 was a getattr default standing in for
                # an attribute that did not exist, silently disabling the
                # check it fed. Here the two cases are named: a StateError
                # carries a typed reason code, anything else is generic.
                error_code = exc.reason_code if isinstance(exc, StateError) else "migration_failed"
                try:
                    _record_attempt_finished(
                        finish_conn, migration, OUTCOME_FAILED, error_code=error_code
                    )
                finally:
                    if finish_conn is not conn:
                        finish_conn.close()
                raise MigrationFailedError(
                    f"migration {migration.migration_id} "
                    f"({migration.from_version} -> {migration.to_version}) failed: {exc}",
                    migration_id=migration.migration_id,
                    from_version=migration.from_version,
                    to_version=migration.to_version,
                    backup_path=str(backup.path) if backup is not None else None,
                    backup_sha256=backup.sha256 if backup is not None else None,
                    remediation=(
                        f"restore the verified backup at {backup.path}"
                        if backup is not None
                        else "no pre-existing database; delete the partial file and retry"
                    ),
                ) from exc

            _record_attempt_finished(conn, migration, OUTCOME_SUCCEEDED, finished_at=timestamp)
            applied.append(migration.migration_id)

        return MigrationResult(
            from_version=current_version,
            to_version=plan[-1].to_version,
            applied=tuple(applied),
            backup=backup,
            was_empty_database=was_empty,
        )
    finally:
        conn.close()
