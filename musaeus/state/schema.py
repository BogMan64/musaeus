"""Schema-version metadata, migration ledger, and the migration registry.

This module owns the *contract*: what ``state_metadata`` and
``schema_migrations`` look like, what "supported" means, and the ordered list
of migrations that take a database from one version to the next. It never
opens a database connection on import and never mutates state on import —
callers explicitly pass an open :class:`sqlite3.Connection`.

Versioning rule (DR-02): the schema version is monotonic. A process must
refuse to operate on a database whose recorded version is newer than the
highest version this code knows about, and it must never attempt a partial
downgrade.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


class SchemaError(RuntimeError):
    """Base class for schema/migration-ledger failures."""


class UnsupportedSchemaVersionError(SchemaError):
    """Raised when a database's recorded schema_version is out of range.

    This covers both "too new" (a future version this code does not know
    about — refuse rather than guess) and "too old" (below the minimum this
    code still carries a migration path for).
    """


class MigrationFailedError(SchemaError):
    """Raised when a migration's ``apply`` callable raises or a checksum-verified
    backup could not be produced before a data-changing migration started."""


# Backup verification failures are defined once, in musaeus.state.backup — the
# module that actually creates and verifies backups. Do not redeclare this
# class here: a second same-named class would not be caught by a caller's
# ``except`` clause targeting the real one, silently missing the failure.


_METADATA_DDL = """
CREATE TABLE IF NOT EXISTS state_metadata (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version          INTEGER NOT NULL,
    min_compatible_version  INTEGER NOT NULL,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    migration_id    TEXT NOT NULL,
    from_version    INTEGER NOT NULL,
    to_version      INTEGER NOT NULL,
    checksum        TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    outcome         TEXT NOT NULL DEFAULT 'started',
    backup_ref      TEXT,
    error_message   TEXT
);
CREATE INDEX IF NOT EXISTS idx_schema_migrations_to_version
    ON schema_migrations(to_version);
CREATE INDEX IF NOT EXISTS idx_schema_migrations_migration_id
    ON schema_migrations(migration_id);
"""
# schema_migrations.migration_id is deliberately NOT UNIQUE: it is an
# append-only audit ledger, and a migration_id can legitimately have more
# than one row over time (e.g. an early 'failed' attempt followed by a
# later 'succeeded' retry). Uniqueness is enforced elsewhere, at the
# Migration registry level (see MIGRATIONS in this module), not on this
# ledger table.


def ensure_metadata_tables(conn: sqlite3.Connection) -> None:
    """Create ``state_metadata``/``schema_migrations`` if they do not exist yet.

    Idempotent and safe to call on every ``open_db()``. Does not itself decide
    or write a schema version — see :func:`stamp_current_version_if_unversioned`.
    """
    conn.executescript(_METADATA_DDL)
    conn.commit()


def read_schema_version(conn: sqlite3.Connection) -> int | None:
    """Return the recorded schema_version, or ``None`` if never stamped.

    A ``None`` result means either a brand-new database (safe to stamp at the
    current version) or a pre-versioning legacy database (must be migrated
    from version 0 — see :data:`MIGRATIONS`).
    """
    row = conn.execute("SELECT schema_version FROM state_metadata WHERE id = 1").fetchone()
    return None if row is None else int(row[0])


def stamp_current_version_if_unversioned(
    conn: sqlite3.Connection, version: int, min_compatible_version: int
) -> None:
    """Write the initial ``state_metadata`` row only when none exists yet.

    This is for brand-new (freshly created, empty) databases only. An
    existing legacy database without this row is version 0 and must go
    through :data:`MIGRATIONS`, not be silently stamped at the current
    version — that would skip real schema work.
    """
    now = _utc_now()
    conn.execute(
        """
        INSERT INTO state_metadata (id, schema_version, min_compatible_version, created_at, updated_at)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (version, min_compatible_version, now, now),
    )
    conn.commit()


def _write_schema_version(
    conn: sqlite3.Connection, version: int, min_compatible_version: int
) -> None:
    now = _utc_now()
    conn.execute(
        """
        INSERT INTO state_metadata (id, schema_version, min_compatible_version, created_at, updated_at)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            schema_version = excluded.schema_version,
            min_compatible_version = excluded.min_compatible_version,
            updated_at = excluded.updated_at
        """,
        (version, min_compatible_version, now, now),
    )


def assert_supported_version(version: int) -> None:
    """Raise :class:`UnsupportedSchemaVersionError` outside the supported range.

    Refuses a database newer than this code understands (forward version)
    and a database older than the oldest version this code still has a
    migration path for (stale, unmigratable legacy state).
    """
    if version > CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"Database schema_version={version} is newer than this application "
            f"supports (max known version={CURRENT_SCHEMA_VERSION}). Refusing to "
            "open — upgrade the application before touching this database."
        )
    if version < MIN_COMPATIBLE_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"Database schema_version={version} is older than the minimum "
            f"version this application can migrate from "
            f"(min={MIN_COMPATIBLE_SCHEMA_VERSION}). Refusing to open."
        )


def list_migration_ledger(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all recorded migration attempts, oldest first."""
    return conn.execute("SELECT * FROM schema_migrations ORDER BY id").fetchall()


@dataclass(frozen=True)
class Migration:
    """One ordered, checksummed schema step.

    ``apply`` receives an open connection already inside the caller's
    transaction/candidate-copy context and must raise on failure rather than
    silently continuing. ``is_transactional`` tells the migration runner
    whether this step is safe to apply inside a single SQLite transaction
    (pure DDL/DML) or whether it requires the offline candidate-copy path
    (e.g. a rewrite that SQLite cannot roll back cleanly, such as certain
    table-rebuild patterns).
    """

    migration_id: str
    from_version: int
    to_version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]
    is_transactional: bool = True


def _migration_0001_baseline_archive_columns(conn: sqlite3.Connection) -> None:
    """Version 0 -> 1: the loudness/ReplayGain/car-export columns.

    This captures the columns that ``musaeus.db._MIGRATIONS`` previously
    applied ad hoc with no version record. Encoding them here gives every
    pre-versioning legacy database (schema_version 0, i.e. never stamped) an
    explicit, checksummed, ledgered path to version 1 instead of relying on
    an unversioned "add column if missing" loop.

    A version-0 database is any unstamped database, not only one that
    already has an ``archive`` table (e.g. a legacy database whose only
    content was the old ``duplicates`` table). Skip as a no-op when
    ``archive`` does not exist rather than letting ``ALTER TABLE`` raise.
    """
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive'"
    ).fetchone()
    if table_exists is None:
        return
    existing = {row[1] for row in conn.execute("PRAGMA table_info(archive)").fetchall()}
    for column, coltype in (
        ("lufs", "REAL"),
        ("lufs_tp", "REAL"),
        ("rg_gain", "REAL"),
        ("rg_peak", "REAL"),
        ("rg_tagged_at", "TEXT"),
        ("car_export_path", "TEXT"),
        ("noise_profile", "TEXT"),
    ):
        if column not in existing:
            conn.execute(f"ALTER TABLE archive ADD COLUMN {column} {coltype}")


def _migration_0002_duplicates_repository_contract(conn: sqlite3.Connection) -> None:
    """Version 1 -> 2: repair the ``duplicates`` table to the DR-07 contract.

    The pre-P0-06 ``duplicates`` table (see ``musaeus/db.py``) used
    ``group_id``/``duplicate_type``/``confidence`` columns that do not match
    the typed AcoustID candidate-record contract required by P0-14
    (``candidate_item_id``, ``matched_item_id``, ``detector``,
    ``provider_recording_id``, ``fingerprint_digest``, ``score``,
    ``evidence_json``, ``decision_status``). Rather than discard the legacy
    rows, this migration renames the old table to
    ``duplicates_legacy_v1`` (preserved losslessly, with provenance) and
    creates the new canonical table fresh. P0-14 will point
    ``insert_acoustid_candidate`` at this table; this migration only
    establishes the schema, and creates zero rows of its own.
    """
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "duplicates" in tables:
        conn.execute("ALTER TABLE duplicates RENAME TO duplicates_legacy_v1")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS duplicates (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id                  TEXT NOT NULL,
            candidate_item_id       TEXT NOT NULL,
            matched_item_id         TEXT NOT NULL,
            detector                TEXT NOT NULL,
            provider_recording_id   TEXT,
            fingerprint_digest      TEXT NOT NULL DEFAULT '',
            score                   REAL,
            evidence_json           TEXT NOT NULL,
            decision_status         TEXT NOT NULL DEFAULT 'pending',
            created_at              TEXT NOT NULL,
            UNIQUE(candidate_item_id, matched_item_id, detector, fingerprint_digest)
        )
        """
    )
    # SQLite treats NULL as distinct in a UNIQUE index, so fingerprint_digest
    # is NOT NULL with a '' default above: a detector reporting no
    # fingerprint still dedupes correctly against the UNIQUE constraint
    # rather than allowing unlimited duplicate rows for the same candidate
    # pair + detector.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_duplicates_run ON duplicates(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_duplicates_status ON duplicates(decision_status)")


# Ordered, immutable migration registry. Append-only: once released, a
# migration's id/from_version/to_version/behaviour must never change —
# add a new migration instead of editing history.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        migration_id="0001_baseline_archive_columns",
        from_version=0,
        to_version=1,
        description="Add loudness/ReplayGain/car-export columns to archive.",
        apply=_migration_0001_baseline_archive_columns,
        is_transactional=True,
    ),
    Migration(
        migration_id="0002_duplicates_repository_contract",
        from_version=1,
        to_version=2,
        description=(
            "Preserve legacy duplicates rows as duplicates_legacy_v1 and create the "
            "canonical AcoustID-ready duplicates table (DR-07 contract)."
        ),
        apply=_migration_0002_duplicates_repository_contract,
        is_transactional=True,
    ),
)

CURRENT_SCHEMA_VERSION: int = max((m.to_version for m in MIGRATIONS), default=0)
MIN_COMPATIBLE_SCHEMA_VERSION: int = 0
