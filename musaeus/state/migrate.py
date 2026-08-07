"""Backup-then-migrate orchestration: transactional and candidate-swap paths.

Two strategies, both described in the design doc (DR-02):

- :func:`migrate_database` — applies pending migrations to *db_path* directly,
  inside a single SQLite transaction per migration, after taking a verified
  backup. Use this when every pending migration is simple DDL/DML that
  SQLite can roll back cleanly.
- :func:`migrate_via_candidate_swap` — copies *db_path* to an offline
  candidate file, applies and validates migrations there, and only replaces
  the live file with ``os.replace`` (atomic on the same filesystem) once the
  candidate passes validation. Use this for a migration that should never be
  attempted against the file a live process might still be reading.

Both strategies detect the current version before touching anything ("detect
before change"), refuse an unsupported/forward version, and never attempt a
downgrade. Both require the caller to supply *backup_dir* explicitly; this
module has no knowledge of and never touches the fixed future live recovery
root.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .backup import VerifiedBackup, create_verified_backup
from .schema import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    MIN_COMPATIBLE_SCHEMA_VERSION,
    Migration,
    MigrationFailedError,
    assert_supported_version,
    ensure_metadata_tables,
    read_schema_version,
    stamp_current_version_if_unversioned,
)

__all__ = [
    "MigrationOutcome",
    "CandidateSwapResult",
    "migrate_database",
    "migrate_via_candidate_swap",
]


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _utc_now_compact() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _migration_checksum(migration: Migration) -> str:
    """Stable identity checksum for a migration's declared contract.

    Hashes the migration's id/from/to/description rather than its Python
    source, so the ledger records *what the migration claimed to do* at the
    version this code ran it. This is enough to detect a migration_id being
    silently redefined between runs (the recorded checksum for a given
    migration_id would then disagree across databases).
    """
    identity = f"{migration.migration_id}:{migration.from_version}:{migration.to_version}:{migration.description}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MigrationOutcome:
    """Result of a completed (possibly no-op) migration run."""

    from_version: int
    to_version: int
    applied: tuple[str, ...]
    backup: VerifiedBackup | None
    already_current: bool = False


@dataclass(frozen=True)
class CandidateSwapResult:
    """Result of a completed candidate-copy-and-swap migration run."""

    from_version: int
    to_version: int
    applied: tuple[str, ...]
    backup: VerifiedBackup | None
    already_current: bool = False


def _detect_current_version(conn: sqlite3.Connection, target_version: int) -> int | None:
    """Return the recorded version, ``0`` for unversioned legacy content, or
    ``None`` for a genuinely brand-new (empty) database that this call stamps
    at *target_version* immediately (no migration path is meaningful for a
    database with no prior content)."""
    ensure_metadata_tables(conn)
    version = read_schema_version(conn)
    if version is not None:
        return version
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if not row[0].startswith("sqlite_")
    }
    # ``schema_migrations`` uses AUTOINCREMENT, which makes SQLite create its
    # own internal ``sqlite_sequence`` bookkeeping table. That table (and any
    # other sqlite_-prefixed internal table) must never count as "legacy
    # application content" — otherwise a genuinely empty database would be
    # misdetected as a pre-versioning legacy database at version 0.
    has_legacy_content = bool(tables - {"state_metadata", "schema_migrations"})
    if has_legacy_content:
        return 0
    stamp_current_version_if_unversioned(conn, target_version, MIN_COMPATIBLE_SCHEMA_VERSION)
    return None


def _pending_migrations(current_version: int, target_version: int) -> list[Migration]:
    """Return the ordered, contiguous migration path from current to target.

    Raises :class:`MigrationFailedError` if the registry does not provide an
    unbroken chain of ``from_version`` steps from *current_version* up to
    *target_version* — a gap must fail loudly rather than silently skip a
    step.
    """
    if current_version == target_version:
        return []
    if current_version > target_version:
        raise MigrationFailedError(
            f"Refusing to migrate database backwards from version "
            f"{current_version} to {target_version}."
        )
    candidates = sorted(
        (m for m in MIGRATIONS if current_version <= m.from_version < target_version),
        key=lambda m: m.from_version,
    )
    cursor = current_version
    path: list[Migration] = []
    for migration in candidates:
        if migration.from_version != cursor:
            continue
        path.append(migration)
        cursor = migration.to_version
    if cursor != target_version:
        raise MigrationFailedError(
            f"No contiguous migration path from version {current_version} to "
            f"{target_version} (reached {cursor}). Registry: "
            f"{[m.migration_id for m in MIGRATIONS]}"
        )
    return path


def _record_migration_attempt(
    conn: sqlite3.Connection, migration: Migration, backup_ref: str | None
) -> int:
    """Insert a 'started' ledger row and return its row id.

    The row id (not ``migration_id``) is what the caller must use to mark
    this specific attempt as succeeded — a retried migration_id can have
    multiple ledger rows over time (e.g. an earlier 'failed' attempt plus
    a later successful one), and updating by migration_id alone would
    silently overwrite that earlier failed row's outcome, erasing the
    audit trail of the original failure.
    """
    started_at = _utc_now()
    checksum = _migration_checksum(migration)
    cursor = conn.execute(
        """
        INSERT INTO schema_migrations
            (migration_id, from_version, to_version, checksum, started_at, outcome, backup_ref)
        VALUES (?, ?, ?, ?, ?, 'started', ?)
        """,
        (
            migration.migration_id,
            migration.from_version,
            migration.to_version,
            checksum,
            started_at,
            backup_ref,
        ),
    )
    assert cursor.lastrowid is not None  # AUTOINCREMENT guarantees a rowid
    return cursor.lastrowid


def _apply_pending_migrations(
    db_path: Path, current_version: int, pending: list[Migration], backup: VerifiedBackup | None
) -> int:
    """Apply *pending* migrations to *db_path* in one transaction; return the
    resulting version. Raises :class:`MigrationFailedError` (with the prior
    database state left intact, since the failing transaction is rolled back)
    if any migration's ``apply`` callable raises."""
    backup_ref = str(backup.backup_path) if backup is not None else None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    version_cursor = current_version
    failed_migration: Migration | None = None
    failure_message = ""
    try:
        conn.execute("BEGIN IMMEDIATE")
        for migration in pending:
            attempt_id = _record_migration_attempt(conn, migration, backup_ref)
            try:
                migration.apply(conn)
            except Exception as exc:  # noqa: BLE001 — re-raised as MigrationFailedError below
                failed_migration = migration
                failure_message = str(exc)
                raise
            conn.execute(
                "UPDATE schema_migrations SET outcome='succeeded', finished_at=? WHERE id=?",
                (_utc_now(), attempt_id),
            )
            version_cursor = migration.to_version
        conn.execute(
            """
            INSERT INTO state_metadata (id, schema_version, min_compatible_version, created_at, updated_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                schema_version = excluded.schema_version,
                min_compatible_version = excluded.min_compatible_version,
                updated_at = excluded.updated_at
            """,
            (version_cursor, MIN_COMPATIBLE_SCHEMA_VERSION, _utc_now(), _utc_now()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        if failed_migration is not None:
            _record_failed_migration_separately(
                db_path, failed_migration, backup_ref, failure_message
            )
            raise MigrationFailedError(
                f"Migration {failed_migration.migration_id} "
                f"({failed_migration.from_version} -> {failed_migration.to_version}) failed: "
                f"{failure_message}. Prior database state was rolled back and left intact."
            ) from None
        raise
    finally:
        # Already closed on the failure path above in some cases; suppress
        # the resulting ProgrammingError rather than tracking closed-state.
        with contextlib.suppress(sqlite3.ProgrammingError):
            conn.close()
    return version_cursor


def _record_failed_migration_separately(
    db_path: Path, migration: Migration, backup_ref: str | None, error_message: str
) -> None:
    """Persist a 'failed' ledger row in its own transaction.

    The migration's own transaction (including its 'started' ledger insert)
    was already rolled back by the caller, so this writes a fresh, minimal
    audit record — outside that rolled-back transaction — purely for
    diagnosis. It never touches application tables.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_metadata_tables(conn)
        conn.execute(
            """
            INSERT INTO schema_migrations
                (migration_id, from_version, to_version, checksum, started_at, finished_at,
                 outcome, backup_ref, error_message)
            VALUES (?, ?, ?, ?, ?, ?, 'failed', ?, ?)
            """,
            (
                migration.migration_id,
                migration.from_version,
                migration.to_version,
                _migration_checksum(migration),
                _utc_now(),
                _utc_now(),
                backup_ref,
                error_message,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def migrate_database(
    db_path: Path, backup_dir: Path, *, target_version: int | None = None
) -> MigrationOutcome:
    """Migrate *db_path* in place, transactionally, taking a backup first.

    Detects the current version before touching anything. If already at
    *target_version* (default: :data:`CURRENT_SCHEMA_VERSION`), this is a
    no-op and no backup is taken. Otherwise: verify a backup exists and
    passes integrity checks, then apply the pending migrations inside one
    transaction. On any migration failure, the transaction rolls back,
    leaving the prior database state usable, and a 'failed' ledger row is
    recorded separately for diagnosis.

    Raises :class:`musaeus.state.schema.UnsupportedSchemaVersionError` for an
    out-of-range recorded version, and :class:`MigrationFailedError` for a
    broken migration path or an ``apply`` failure.
    """
    target = CURRENT_SCHEMA_VERSION if target_version is None else target_version
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        current_version = _detect_current_version(conn, target)
    finally:
        conn.close()

    if current_version is None:
        return MigrationOutcome(
            from_version=target, to_version=target, applied=(), backup=None, already_current=True
        )
    assert_supported_version(current_version)
    pending = _pending_migrations(current_version, target)
    if not pending:
        return MigrationOutcome(
            from_version=current_version,
            to_version=target,
            applied=(),
            backup=None,
            already_current=True,
        )

    backup = create_verified_backup(db_path, backup_dir)
    final_version = _apply_pending_migrations(db_path, current_version, pending, backup)
    return MigrationOutcome(
        from_version=current_version,
        to_version=final_version,
        applied=tuple(m.migration_id for m in pending),
        backup=backup,
    )


def _flush_wal_and_checkpoint(db_path: Path) -> None:
    """Force a WAL checkpoint and merge, leaving *db_path* self-contained.

    Switches the connection's journal mode to DELETE, which forces SQLite to
    checkpoint and remove any ``-wal``/``-shm`` sidecar files, then restores
    WAL mode so normal MUSAEUS operation is unaffected. Required before a
    raw file copy (this module's candidate-swap path) or an ``os.replace``,
    since a stale sidecar pointing at a since-replaced main file is unsafe.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
    finally:
        conn.close()


def _validate_candidate(candidate_path: Path, target_version: int) -> None:
    """Validate a candidate database before it is allowed to replace the live file.

    Runs ``PRAGMA integrity_check`` and confirms the candidate's recorded
    schema_version matches *target_version* exactly. Raises
    :class:`MigrationFailedError` on any failure; the caller is responsible
    for discarding the candidate file in that case.
    """
    uri = f"file:{quote(candidate_path.as_posix(), safe='/:')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
        if rows != ["ok"]:
            raise MigrationFailedError(f"Candidate database failed integrity_check: {rows}")
        candidate_version = read_schema_version(conn)
    finally:
        conn.close()
    if candidate_version != target_version:
        raise MigrationFailedError(
            f"Candidate database schema_version={candidate_version} does not match "
            f"expected target_version={target_version}."
        )


def migrate_via_candidate_swap(
    db_path: Path,
    backup_dir: Path,
    work_dir: Path,
    *,
    target_version: int | None = None,
) -> CandidateSwapResult:
    """Migrate via an offline candidate copy, validated before an atomic swap.

    1. Detect the current version against *db_path* (no mutation yet).
    2. Take a verified backup of *db_path* in *backup_dir*.
    3. Flush *db_path*'s WAL and copy it to a candidate file in *work_dir*.
    4. Apply pending migrations to the candidate only.
    5. Validate the candidate (`PRAGMA integrity_check` + version check).
    6. Flush the candidate's WAL and atomically ``os.replace`` it onto
       *db_path*.

    If any step from 3 onward fails, *db_path* is never touched — the
    candidate file is discarded and the original error propagates. Requires
    *backup_dir* and *work_dir* to be supplied explicitly; performs no
    fallback to any hardcoded or live recovery path.
    """
    target = CURRENT_SCHEMA_VERSION if target_version is None else target_version
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        current_version = _detect_current_version(conn, target)
    finally:
        conn.close()

    if current_version is None:
        return CandidateSwapResult(
            from_version=target, to_version=target, applied=(), backup=None, already_current=True
        )
    assert_supported_version(current_version)
    pending = _pending_migrations(current_version, target)
    if not pending:
        return CandidateSwapResult(
            from_version=current_version,
            to_version=target,
            applied=(),
            backup=None,
            already_current=True,
        )

    backup = create_verified_backup(db_path, backup_dir)

    work_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = work_dir / f"{db_path.stem}.candidate.{_utc_now_compact()}{db_path.suffix}"
    _flush_wal_and_checkpoint(db_path)
    shutil.copy2(db_path, candidate_path)

    def _remove_candidate_and_sidecars() -> None:
        candidate_path.unlink(missing_ok=True)
        for sidecar_suffix in ("-wal", "-shm"):
            sidecar = candidate_path.with_name(candidate_path.name + sidecar_suffix)
            sidecar.unlink(missing_ok=True)

    try:
        final_version = _apply_pending_migrations(candidate_path, current_version, pending, backup)
        _validate_candidate(candidate_path, final_version)
        # _flush_wal_and_checkpoint leaves the candidate in WAL mode again
        # (matching normal MUSAEUS operation), which can recreate -wal/-shm
        # sidecars for the *candidate* name. os.replace only moves the main
        # file, so those sidecars must be cleaned up here on the success
        # path too, not only in the except branch below.
        _flush_wal_and_checkpoint(candidate_path)
        os.replace(candidate_path, db_path)
        for sidecar_suffix in ("-wal", "-shm"):
            sidecar = candidate_path.with_name(candidate_path.name + sidecar_suffix)
            sidecar.unlink(missing_ok=True)
    except Exception:
        _remove_candidate_and_sidecars()
        raise

    return CandidateSwapResult(
        from_version=current_version,
        to_version=final_version,
        applied=tuple(m.migration_id for m in pending),
        backup=backup,
    )
