"""
MUSAEUS — versioned state schema (P0-06)

Owns the schema-version declaration, the `state_metadata` and
`schema_migrations` tables, compatibility checking, and read-only
detection. See DR-02.

Why a new module rather than more of `db.py`'s `_MIGRATIONS` list: that
list is an idempotent "ALTER TABLE ADD COLUMN if the column is missing"
loop with no version, no ledger, no ordering guarantee and no failure
path. It cannot express "this database is newer than I understand", it
cannot record that a migration was attempted and failed, and re-running
it on a half-migrated database is indistinguishable from running it on a
fresh one. That is exactly the silent-no-op shape this project keeps
finding: it always reports success.

This module deliberately does NOT change `db.py`'s behaviour. Stamping a
version into a database is itself a mutation, and P0's rule is that no
mutation happens outside a preflight-gated, checkpointed path. Wiring
happens in P0-11; until then `open_db()` is untouched and the live
pipeline's behaviour is unchanged.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ── Version declarations ──────────────────────────────────────────────────────

# The schema version this application writes and understands.
SCHEMA_VERSION: int = 1

# The oldest schema version this application can migrate forward from.
# 0 is the sentinel for a legacy, unversioned MUSAEUS database -- one
# created by `db.py`'s `open_db()` before P0-06 existed. Such a database
# has `events`/`archive` tables but no `state_metadata`, so its version
# cannot be read; it is *inferred* as 0.
MIN_SUPPORTED_SCHEMA: int = 0

# Sentinel for "this database has no version stamp".
LEGACY_UNVERSIONED: int = 0


# ── Typed errors ──────────────────────────────────────────────────────────────


class StateError(RuntimeError):
    """Base for state-layer failures. Every subclass carries a reason code
    from the design's typed-error list so reports and exit statuses do not
    have to pattern-match on message text."""

    reason_code = "state_error"

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.details = details


class SchemaIncompatibleError(StateError):
    """The database's schema version is outside what this build supports.

    Raised for a FORWARD version (database newer than the application).
    Fail-closed: the caller must not proceed to any content mutation, and
    must not attempt a partial downgrade (DR-02)."""

    reason_code = "schema_incompatible"


class DatabaseReadOnlyError(StateError):
    """The database (or the directory it lives in) cannot be written."""

    reason_code = "database_read_only"


class MigrationLedgerError(StateError):
    """The migration ledger is in a state that blocks further work -- most
    importantly an attempt recorded as started but never finished."""

    reason_code = "migration_incomplete"


# ── State tables ──────────────────────────────────────────────────────────────

# `state_metadata` is a single-row table (enforced by the CHECK) rather
# than SQLite's built-in `PRAGMA user_version`. user_version holds one
# bare integer with no room for the compatibility range or the
# created/updated timestamps DR-02 requires, and -- decisively -- it is
# invisible to `.dump`/iterdump, which is how P0-05's before/after
# equality artefacts hash database content. A version living somewhere
# the equality check cannot see is a version that can drift silently.
STATE_TABLES_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS state_metadata (
        id              INTEGER PRIMARY KEY CHECK (id = 1),
        schema_version  INTEGER NOT NULL,
        app_min_schema  INTEGER NOT NULL,
        app_max_schema  INTEGER NOT NULL,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        migration_id  TEXT PRIMARY KEY,
        from_version  INTEGER NOT NULL,
        to_version    INTEGER NOT NULL,
        checksum      TEXT NOT NULL,
        started_at    TEXT NOT NULL,
        finished_at   TEXT,
        outcome       TEXT NOT NULL,
        error_code    TEXT,
        backup_ref    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_migrations_outcome ON schema_migrations(outcome)",
)

# Kept as a joined script for readability/diagnostics only. Nothing that
# needs transactional behaviour may use it: sqlite3.executescript() issues
# an implicit COMMIT before it runs, which would quietly commit -- and so
# make unrollbackable -- the very migration the caller wrapped in a
# transaction. Migrations execute STATE_TABLES_STATEMENTS one statement at
# a time through execute() instead.
STATE_TABLES_DDL: str = ";\n".join(s.strip() for s in STATE_TABLES_STATEMENTS) + ";"


# Ledger outcome vocabulary. Closed, like the event vocabulary in DR-02.
OUTCOME_RUNNING: str = "running"
OUTCOME_SUCCEEDED: str = "succeeded"
OUTCOME_FAILED: str = "failed"
LEDGER_OUTCOMES: frozenset[str] = frozenset({OUTCOME_RUNNING, OUTCOME_SUCCEEDED, OUTCOME_FAILED})


@dataclass(frozen=True)
class StateMetadata:
    """The single `state_metadata` row, as read."""

    schema_version: int
    app_min_schema: int
    app_max_schema: int
    created_at: str
    updated_at: str


def utc_now_iso() -> str:
    """UTC ISO-8601, second resolution, explicit `Z`."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Reading state ─────────────────────────────────────────────────────────────


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """True when *name* exists as a table in this database."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def is_empty_database(conn: sqlite3.Connection) -> bool:
    """True for a database with no user tables at all -- a brand-new file.

    Distinguished from a legacy database (which has `events`/`archive` but
    no `state_metadata`) because the two need different treatment: a fresh
    database is *created* at the current version, a legacy one is
    *migrated* to it, and conflating them is how a half-migrated database
    gets stamped as if it had always been current."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()
    return int(row[0]) == 0


def read_schema_version(conn: sqlite3.Connection) -> int:
    """
    Return the recorded schema version, or LEGACY_UNVERSIONED (0) when the
    database has no `state_metadata` table or no row in it.

    Deliberately not written as `getattr(...)`/`.get(..., default)` over a
    maybe-missing structure. OPEN_ITEMS finding #6 was exactly that shape:
    a default silently stood in for a missing attribute and the check it
    fed could never fire. Here the missing case is an explicit branch with
    a named sentinel, so "unversioned" is a value the caller must handle,
    not a zero that looks like an answer.
    """
    if not table_exists(conn, "state_metadata"):
        return LEGACY_UNVERSIONED
    row = conn.execute("SELECT schema_version FROM state_metadata WHERE id = 1").fetchone()
    if row is None:
        return LEGACY_UNVERSIONED
    return int(row[0])


def read_state_metadata(conn: sqlite3.Connection) -> StateMetadata | None:
    """Return the full `state_metadata` row, or None when unversioned."""
    if not table_exists(conn, "state_metadata"):
        return None
    row = conn.execute(
        "SELECT schema_version, app_min_schema, app_max_schema, created_at, updated_at "
        "FROM state_metadata WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    return StateMetadata(
        schema_version=int(row[0]),
        app_min_schema=int(row[1]),
        app_max_schema=int(row[2]),
        created_at=str(row[3]),
        updated_at=str(row[4]),
    )


# ── Writing state ─────────────────────────────────────────────────────────────


def ensure_state_tables(conn: sqlite3.Connection) -> None:
    """Create the state tables if absent. Does not stamp a version.

    One execute() per statement, never executescript() -- see the note on
    STATE_TABLES_DDL."""
    for statement in STATE_TABLES_STATEMENTS:
        conn.execute(statement)


def stamp_schema_version(conn: sqlite3.Connection, version: int, *, now: str | None = None) -> None:
    """
    Record *version* in `state_metadata`, creating the row on first stamp
    and preserving the original `created_at` on later ones.
    """
    timestamp = now if now is not None else utc_now_iso()
    conn.execute(
        """
        INSERT INTO state_metadata
            (id, schema_version, app_min_schema, app_max_schema, created_at, updated_at)
        VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            schema_version = excluded.schema_version,
            app_min_schema = excluded.app_min_schema,
            app_max_schema = excluded.app_max_schema,
            updated_at     = excluded.updated_at
        """,
        (version, MIN_SUPPORTED_SCHEMA, SCHEMA_VERSION, timestamp, timestamp),
    )


# ── Compatibility ─────────────────────────────────────────────────────────────


def check_compatibility(version: int, *, db_path: Path | None = None) -> None:
    """
    Raise SchemaIncompatibleError when *version* is outside what this build
    can work with.

    Forward versions (database newer than the application) are refused
    outright: a newer MUSAEUS may have written tables, columns or event
    payloads this build would misread, and DR-02 forbids attempting a
    partial downgrade. Older-but-supported versions are not an error --
    they are the input to a migration.
    """
    if version > SCHEMA_VERSION:
        raise SchemaIncompatibleError(
            f"database schema version {version} is newer than this build understands "
            f"(supported: {MIN_SUPPORTED_SCHEMA}..{SCHEMA_VERSION}); "
            f"refusing to open or downgrade it",
            found_version=version,
            supported_min=MIN_SUPPORTED_SCHEMA,
            supported_max=SCHEMA_VERSION,
            db_path=str(db_path) if db_path is not None else None,
            remediation="upgrade MUSAEUS, or point at a database written by this build",
        )
    if version < MIN_SUPPORTED_SCHEMA:
        raise SchemaIncompatibleError(
            f"database schema version {version} is older than this build can migrate "
            f"(supported: {MIN_SUPPORTED_SCHEMA}..{SCHEMA_VERSION})",
            found_version=version,
            supported_min=MIN_SUPPORTED_SCHEMA,
            supported_max=SCHEMA_VERSION,
            db_path=str(db_path) if db_path is not None else None,
            remediation="restore a backup written by a supported build",
        )


def check_ledger_clean(conn: sqlite3.Connection, *, db_path: Path | None = None) -> None:
    """
    Raise MigrationLedgerError when the ledger holds an attempt that
    started and never finished.

    This is the fail-closed half of the ledger. A process killed mid
    migration (the 12-hour-file OOM in OPEN_ITEMS is the local precedent
    for "killed mid-run") leaves a `running` row behind. Without this
    check the next run would see a plausible-looking database, re-apply
    from whatever version it reads, and report success over an unknown
    partial state.
    """
    if not table_exists(conn, "schema_migrations"):
        return
    rows = conn.execute(
        "SELECT migration_id, from_version, to_version, started_at FROM schema_migrations "
        "WHERE outcome = ? ORDER BY migration_id",
        (OUTCOME_RUNNING,),
    ).fetchall()
    if not rows:
        return
    stuck = [str(r[0]) for r in rows]
    raise MigrationLedgerError(
        f"migration ledger has {len(stuck)} attempt(s) recorded as started but never "
        f"finished: {', '.join(stuck)}; refusing to proceed over an unknown partial state",
        stuck_migrations=stuck,
        db_path=str(db_path) if db_path is not None else None,
        remediation="restore the verified backup recorded against the stuck attempt",
    )


# ── Read-only detection ───────────────────────────────────────────────────────


def detect_read_only(db_path: Path) -> bool:
    """
    True when this process cannot write *db_path*.

    Two checks, because either alone gives a wrong answer:

    * Filesystem permissions on the file AND its parent directory. The
      directory matters independently: SQLite in WAL mode must create
      `-wal` and `-shm` sidecars next to the database, so a writable file
      in an unwritable directory is still effectively read-only.
    * A `BEGIN IMMEDIATE` probe, which actually acquires the write lock
      and is immediately rolled back. Permission bits can say yes while a
      read-only mount, an immutable attribute, or an ACL says no.

    The probe is the effect-based half: it fails for the same reason a
    real write would, rather than describing the shape of a write.
    """
    if not db_path.exists():
        # A database that does not exist yet is writable iff its directory is.
        parent = db_path.parent
        return not (parent.is_dir() and os.access(parent, os.W_OK))

    if not os.access(db_path, os.W_OK):
        return True
    if not os.access(db_path.parent, os.W_OK):
        return True

    try:
        probe = sqlite3.connect(str(db_path), timeout=5)
    except sqlite3.Error:
        return True
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("ROLLBACK")
    except sqlite3.Error:
        return True
    finally:
        probe.close()
    return False
