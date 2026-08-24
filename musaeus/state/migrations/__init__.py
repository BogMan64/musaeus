"""
MUSAEUS — ordered schema migrations (P0-06)

A migration is data, not a free function: an id, the version it moves
from, the version it moves to, the exact SQL it runs, and whether that
SQL can run inside one transaction. Everything the migrator needs to
decide, log, and check is therefore inspectable without executing
anything -- including the checksum, which is derived from the declared
SQL rather than from a Python callable's bytecode (bytecode changes when
an unrelated line above it moves; the SQL does not).

Registry rules, enforced by `validate_registry()` and asserted in tests
rather than assumed:

* ids are unique and sort in application order;
* versions are strictly increasing and contiguous -- migration N's
  `to_version` is migration N+1's `from_version`, with no gaps;
* the chain starts at LEGACY_UNVERSIONED and ends at SCHEMA_VERSION.

The last rule is the one that keeps the declaration honest. Bumping
SCHEMA_VERSION without adding the migration that reaches it, or adding a
migration without bumping SCHEMA_VERSION, fails at import-time
validation instead of at some later run against a real database.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from musaeus.state.duplicates import DUPLICATES_STATEMENTS
from musaeus.state.events import CANONICAL_EVENT_STATEMENTS
from musaeus.state.projector import PROJECTION_STATEMENTS
from musaeus.state.schema import (
    LEGACY_UNVERSIONED,
    SCHEMA_VERSION,
    STATE_TABLES_STATEMENTS,
    StateError,
)


class MigrationRegistryError(StateError):
    """The declared migration chain is not internally consistent."""

    reason_code = "migration_registry_invalid"


@dataclass(frozen=True)
class Migration:
    """One ordered, checksummed schema change."""

    migration_id: str
    from_version: int
    to_version: int
    statements: tuple[str, ...]
    description: str
    # True  -> apply inside one transaction on the active database;
    #          a failure rolls back and leaves the prior state usable.
    # False -> apply to a copied candidate database, validate it, then
    #          atomically swap it in (DR-02). Used for changes SQLite
    #          cannot roll back, e.g. VACUUM or a journal-mode change.
    transactional: bool = True

    @property
    def checksum(self) -> str:
        """SHA-256 over the migration's identity and its exact SQL.

        Recorded in the ledger so a later build can tell that a migration
        with a familiar id ran different SQL than it now declares."""
        digest = hashlib.sha256()
        digest.update(self.migration_id.encode("utf-8"))
        for statement in self.statements:
            digest.update(b"\x00")
            digest.update(statement.encode("utf-8"))
        return digest.hexdigest()


# ── The chain ─────────────────────────────────────────────────────────────────

M0001_BASELINE = Migration(
    migration_id="0001_baseline_state_tables",
    from_version=LEGACY_UNVERSIONED,
    to_version=1,
    statements=STATE_TABLES_STATEMENTS,
    description=(
        "Introduce state_metadata and schema_migrations. Version 1 is defined as "
        "the schema db.open_db() already produces, plus these two tables. The "
        "existing events/archive/duplicates/metadata_cache tables are deliberately "
        "left exactly as they are: this migration adds the ability to *know* what "
        "version a database is, and changes nothing about what it holds."
    ),
)

M0002_CANONICAL_EVENTS = Migration(
    migration_id="0002_canonical_events_and_projection",
    from_version=1,
    to_version=2,
    statements=(*CANONICAL_EVENT_STATEMENTS, *PROJECTION_STATEMENTS),
    description=(
        "Add the canonical event store and the three derived projection tables. "
        "Additive only: the legacy `events` table is untouched and keeps its role as "
        "a human-readable audit trail. It is deliberately NOT migrated into "
        "canonical_events -- rebuild.py's investigation established that its payloads "
        "are lossy (hashes truncated to 16 chars plus an ellipsis; album/genre/year/"
        "track/duration/codec never recorded), so copying it across would manufacture "
        "a source of truth out of evidence that cannot support one. Legacy rows are "
        "adapted on demand, and anything unmappable is preserved as legacy.unmapped "
        "and blocks the affected run's rebuild."
    ),
)

M0003_DUPLICATES_CONTRACT = Migration(
    migration_id="0003_duplicates_contract",
    from_version=2,
    to_version=3,
    statements=(
        # A brand-new database has no legacy table to rename, and SQLite
        # has no ALTER TABLE ... RENAME IF EXISTS. Declaring the legacy
        # shape first gives the rename a target in both cases, so a fresh
        # database and a legacy one converge on the same structure --
        # `duplicates_legacy` present and empty rather than absent. A
        # migration whose result depends on which kind of database it met
        # is a migration with two outcomes to reason about.
        """
        CREATE TABLE IF NOT EXISTS duplicates (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id        TEXT NOT NULL,
            file_path       TEXT NOT NULL,
            duplicate_type  TEXT,
            confidence      REAL,
            status          TEXT DEFAULT 'pending',
            run_id          TEXT,
            staged_at       TEXT DEFAULT (datetime('now'))
        )
        """,
        # The legacy table is RENAMED, never dropped. DR-07: "it does not
        # silently discard old rows." Its rows stay exactly as they were,
        # readable, under a name that says what they are.
        "ALTER TABLE duplicates RENAME TO duplicates_legacy",
        *DUPLICATES_STATEMENTS,
        # Carry legacy rows across with their original values preserved in
        # the evidence payload. group_id/file_path/duplicate_type have no
        # lossless mapping onto the candidate/matched pair contract -- a
        # legacy row names one file in a group, not a pair -- so the row is
        # brought over as its own compatibility record rather than being
        # guessed into a pair that was never recorded.
        """
        INSERT OR IGNORE INTO duplicates
            (run_id, candidate_item_id, matched_item_id, detector,
             provider_recording_id, fingerprint_digest, score,
             evidence_json, decision_status, created_at, evidence_identity)
        SELECT
            COALESCE(run_id, 'legacy'),
            'legacy:' || group_id,
            'legacy:' || file_path,
            'legacy_' || COALESCE(duplicate_type, 'unknown'),
            NULL,
            NULL,
            confidence,
            json_object(
                'algorithm', 'legacy',
                'provider', 'musaeus_legacy_duplicates',
                'compatibility', json_object(
                    'group_id', group_id,
                    'file_path', file_path,
                    'duplicate_type', duplicate_type,
                    'status', status,
                    'staged_at', staged_at
                )
            ),
            'pending',
            COALESCE(staged_at, '1970-01-01T00:00:00Z'),
            'legacy:' || CAST(id AS TEXT)
        FROM duplicates_legacy
        """,
    ),
    description=(
        "Replace the ad-hoc duplicates table with DR-07's typed contract. The legacy "
        "table is renamed rather than dropped and every row is carried across into a "
        "documented compatibility payload."
    ),
)

MIGRATIONS: tuple[Migration, ...] = (
    M0001_BASELINE,
    M0002_CANONICAL_EVENTS,
    M0003_DUPLICATES_CONTRACT,
)


# ── Registry validation and planning ──────────────────────────────────────────


def validate_registry(migrations: tuple[Migration, ...] = MIGRATIONS) -> None:
    """Raise MigrationRegistryError unless the chain satisfies every rule
    in the module docstring."""
    if not migrations:
        raise MigrationRegistryError("migration registry is empty")

    ids = [m.migration_id for m in migrations]
    if len(set(ids)) != len(ids):
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        raise MigrationRegistryError(
            f"duplicate migration id(s): {', '.join(duplicates)}", duplicate_ids=duplicates
        )
    if ids != sorted(ids):
        raise MigrationRegistryError(
            "migration ids do not sort into application order", declared_order=ids
        )

    for migration in migrations:
        if migration.to_version <= migration.from_version:
            raise MigrationRegistryError(
                f"{migration.migration_id} is not monotonic: "
                f"{migration.from_version} -> {migration.to_version}",
                migration_id=migration.migration_id,
            )

    expected_from = migrations[0].from_version
    for migration in migrations:
        if migration.from_version != expected_from:
            raise MigrationRegistryError(
                f"gap in migration chain before {migration.migration_id}: expected "
                f"from_version {expected_from}, declared {migration.from_version}",
                migration_id=migration.migration_id,
                expected_from=expected_from,
            )
        expected_from = migration.to_version

    if migrations is MIGRATIONS:
        if migrations[0].from_version != LEGACY_UNVERSIONED:
            raise MigrationRegistryError(
                f"chain must start at LEGACY_UNVERSIONED ({LEGACY_UNVERSIONED}), "
                f"starts at {migrations[0].from_version}"
            )
        if migrations[-1].to_version != SCHEMA_VERSION:
            raise MigrationRegistryError(
                f"chain ends at version {migrations[-1].to_version} but SCHEMA_VERSION "
                f"is {SCHEMA_VERSION}; add the missing migration or correct the constant",
                chain_end=migrations[-1].to_version,
                schema_version=SCHEMA_VERSION,
            )


def plan_migrations(
    current_version: int, migrations: tuple[Migration, ...] = MIGRATIONS
) -> tuple[Migration, ...]:
    """
    Return the migrations that move *current_version* to the end of the
    chain, in application order. Empty when already current.

    A version that sits between two migrations -- i.e. matches no
    `from_version` -- is a registry/database disagreement, not something
    to round down to the nearest earlier migration.
    """
    validate_registry(migrations)
    target = migrations[-1].to_version
    if current_version == target:
        return ()
    remaining = tuple(m for m in migrations if m.from_version >= current_version)
    if not remaining or remaining[0].from_version != current_version:
        raise MigrationRegistryError(
            f"no migration declares from_version {current_version}; the database sits "
            f"between declared versions and cannot be migrated forward safely",
            current_version=current_version,
            declared_from_versions=[m.from_version for m in migrations],
        )
    return remaining
