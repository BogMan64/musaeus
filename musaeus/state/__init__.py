"""
MUSAEUS — versioned state layer (P0-06 onward)

Deliberately importable without side effects: no database is opened, no
directory created, no configuration read. Importing this package must
never be the thing that changes something on disk.
"""

from __future__ import annotations

from musaeus.state.migrator import (
    BackupRef,
    BackupVerificationError,
    MigrationFailedError,
    MigrationResult,
    RecoveryTargetError,
    create_verified_backup,
    migrate,
    restore_from_backup,
)
from musaeus.state.policy import (
    FUTURE_RECOVERY_ROOT,
    RECOVERY_CAP_BYTES,
    RECOVERY_CAP_LABEL,
    describe_recovery_policy,
)
from musaeus.state.schema import (
    LEGACY_UNVERSIONED,
    MIN_SUPPORTED_SCHEMA,
    SCHEMA_VERSION,
    DatabaseReadOnlyError,
    MigrationLedgerError,
    SchemaIncompatibleError,
    StateError,
    StateMetadata,
    check_compatibility,
    check_ledger_clean,
    detect_read_only,
    read_schema_version,
    read_state_metadata,
)

__all__ = [
    "FUTURE_RECOVERY_ROOT",
    "LEGACY_UNVERSIONED",
    "MIN_SUPPORTED_SCHEMA",
    "RECOVERY_CAP_BYTES",
    "RECOVERY_CAP_LABEL",
    "SCHEMA_VERSION",
    "BackupRef",
    "BackupVerificationError",
    "DatabaseReadOnlyError",
    "MigrationFailedError",
    "MigrationLedgerError",
    "MigrationResult",
    "RecoveryTargetError",
    "SchemaIncompatibleError",
    "StateError",
    "StateMetadata",
    "check_compatibility",
    "check_ledger_clean",
    "create_verified_backup",
    "describe_recovery_policy",
    "detect_read_only",
    "migrate",
    "read_schema_version",
    "read_state_metadata",
    "restore_from_backup",
]
