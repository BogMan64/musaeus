"""Versioned state schema, migration ledger, and backup/candidate-swap primitives.

Implements MUSAEUS Consumer Readiness task P0-06 (see
``.kiro/specs/musaeus-consumer-readiness/tasks.md`` and design doc DR-02):

- A monotonic ``schema_version`` recorded in ``state_metadata``, with an
  applied-migration ledger in ``schema_migrations``.
- A verified, checksummed backup taken before any data-changing migration.
- Two migration strategies: an in-place transactional path
  (:func:`musaeus.state.migrate.migrate_database`) for changes SQLite can
  apply and roll back atomically, and an offline candidate-copy path
  (:func:`musaeus.state.migrate.migrate_via_candidate_swap`) for changes that
  should be validated before they ever touch the live file.

Policy boundary (binding — see the design doc's confirmed policy table):
this package never creates or probes the fixed future live recovery root
(``/home/grey/Projects/MUSAEUS_RECOVERY``). Every function here requires an
explicit, caller-supplied ``backup_dir``; there is no hardcoded fallback path
and no live-authority side effect.
"""

from __future__ import annotations

from .backup import (
    BackupVerificationError,
    VerifiedBackup,
    create_verified_backup,
    restore_from_backup,
)
from .migrate import (
    CandidateSwapResult,
    MigrationOutcome,
    migrate_database,
    migrate_via_candidate_swap,
)
from .schema import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    MIN_COMPATIBLE_SCHEMA_VERSION,
    Migration,
    MigrationFailedError,
    SchemaError,
    UnsupportedSchemaVersionError,
    assert_supported_version,
    ensure_metadata_tables,
    list_migration_ledger,
    read_schema_version,
    stamp_current_version_if_unversioned,
)

__all__ = [
    "VerifiedBackup",
    "create_verified_backup",
    "restore_from_backup",
    "CandidateSwapResult",
    "MigrationOutcome",
    "migrate_database",
    "migrate_via_candidate_swap",
    "CURRENT_SCHEMA_VERSION",
    "MIN_COMPATIBLE_SCHEMA_VERSION",
    "Migration",
    "MIGRATIONS",
    "BackupVerificationError",
    "MigrationFailedError",
    "SchemaError",
    "UnsupportedSchemaVersionError",
    "assert_supported_version",
    "ensure_metadata_tables",
    "list_migration_ledger",
    "read_schema_version",
    "stamp_current_version_if_unversioned",
]
