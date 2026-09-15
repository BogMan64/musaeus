"""
MUSAEUS — canonical event contract (P0-07)

DR-02 defines a closed event vocabulary with a validated envelope. This
module owns it: the envelope, the vocabulary, per-type required payload
fields, append rules, and the adapter that maps the legacy `events` table
into it.

Why a NEW table rather than a reinterpretation of `events`:

`rebuild.py` is disabled, and its docstring says why in detail -- the
existing `events` table is a human-readable audit trail, lossy by design.
Hashes are stored truncated to 16 characters plus an ellipsis; album,
genre, year, track, duration, sample_rate, channels and codec are never
recorded at all. No amount of renaming makes that table a source of
truth. Building the canonical contract on top of it would repeat exactly
the mistake that module was disabled for, and this time the mistake would
be blessed by a spec.

So `canonical_events` is a separate, additive store with a validated
envelope, and the legacy table is treated as *evidence* -- adaptable
where its meaning is unambiguous, and preserved as `legacy.unmapped`
where it is not.

The consequence is deliberate and is the honest encoding of what
rebuild.py found: of the 43 legacy event types this codebase emits, only
the three run/stage lifecycle types carry meaning the canonical
projection can reconstruct. The other 40 are per-file content evidence
whose payloads are lossy. They do not silently vanish and they are not
guessed into a success -- they become `legacy.unmapped` and BLOCK the
affected run's resume/rebuild. "We cannot reconstruct this" is a result;
"we reconstructed it" would be a lie, and it is the lie that would have
deleted 27,000 archive rows.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any

from musaeus.state.schema import SCHEMA_VERSION, StateError, utc_now_iso

# The application version stamped into every event. Bumped when the
# meaning of a payload changes, not when unrelated code changes.
PIPELINE_VERSION: str = "musaeus-p0"

# Envelope version for the payload schemas declared below.
EVENT_VERSION: int = 1


# ── Closed vocabulary ─────────────────────────────────────────────────────────

RUN_CREATED = "run.created"
RUN_PREFLIGHT_COMPLETED = "run.preflight.completed"
RUN_CANCELLATION_REQUESTED = "run.cancellation_requested"
RUN_TERMINAL = "run.terminal"

STAGE_QUEUED = "stage.queued"
STAGE_STARTED = "stage.started"
STAGE_SUCCEEDED = "stage.succeeded"
STAGE_FAILED = "stage.failed"
STAGE_CANCELLED = "stage.cancelled"
STAGE_BLOCKED = "stage.blocked"

CHECKPOINT_CREATED = "checkpoint.created"
CHECKPOINT_VERIFIED = "checkpoint.verified"

MUTATION_PLANNED = "mutation.planned"
MUTATION_APPLIED = "mutation.applied"
MUTATION_QUARANTINED = "mutation.quarantined"

ROLLBACK_STARTED = "rollback.started"
ROLLBACK_ITEM_RESTORED = "rollback.item_restored"
ROLLBACK_COMPLETED = "rollback.completed"

STATE_MIGRATION_STARTED = "state.migration.started"
STATE_MIGRATION_SUCCEEDED = "state.migration.succeeded"
STATE_MIGRATION_FAILED = "state.migration.failed"

STATE_REBUILD_STARTED = "state.rebuild.started"
STATE_REBUILD_SUCCEEDED = "state.rebuild.succeeded"
STATE_REBUILD_FAILED = "state.rebuild.failed"

LEGACY_UNMAPPED = "legacy.unmapped"

# Required payload fields, transcribed from DR-02's table. The vocabulary
# is closed: an event type absent from this mapping cannot be appended.
REQUIRED_PAYLOAD_FIELDS: dict[str, frozenset[str]] = {
    RUN_CREATED: frozenset({"mode", "config_digest", "scope_summary", "authority"}),
    RUN_PREFLIGHT_COMPLETED: frozenset(
        {"outcome", "checks", "database_version", "recovery_target", "lock_observation"}
    ),
    RUN_CANCELLATION_REQUESTED: frozenset({"requested_by", "requested_at", "reason_code"}),
    RUN_TERMINAL: frozenset(
        {"status", "exit_code", "reason_code", "stage_counts", "checkpoint_id", "rollback_status"}
    ),
    STAGE_QUEUED: frozenset({"stage_id", "attempt", "dependencies", "input_digest"}),
    STAGE_STARTED: frozenset({"stage_id", "attempt", "input_digest"}),
    STAGE_SUCCEEDED: frozenset({"stage_id", "attempt", "input_digest", "output_digest", "counts"}),
    STAGE_FAILED: frozenset(
        {"stage_id", "attempt", "error_code", "safe_to_retry", "checkpoint_id"}
    ),
    STAGE_CANCELLED: frozenset({"stage_id", "attempt", "safe_checkpoint", "checkpoint_id"}),
    STAGE_BLOCKED: frozenset({"stage_id", "attempt", "blockers", "recovery_action"}),
    CHECKPOINT_CREATED: frozenset(
        {"checkpoint_id", "manifest_digest", "coverage", "recovery_target"}
    ),
    CHECKPOINT_VERIFIED: frozenset(
        {"checkpoint_id", "manifest_digest", "coverage", "recovery_target"}
    ),
    MUTATION_PLANNED: frozenset({"operation_id", "item_ref", "operation_kind"}),
    MUTATION_APPLIED: frozenset(
        {"operation_id", "item_ref", "operation_kind", "precondition_digest", "result_digest"}
    ),
    MUTATION_QUARANTINED: frozenset(
        {"operation_id", "item_ref", "operation_kind", "quarantine_ref"}
    ),
    ROLLBACK_STARTED: frozenset({"checkpoint_id", "outcome", "remaining_operations"}),
    ROLLBACK_ITEM_RESTORED: frozenset(
        {"checkpoint_id", "operation_id", "outcome", "remaining_operations"}
    ),
    ROLLBACK_COMPLETED: frozenset({"checkpoint_id", "outcome", "remaining_operations"}),
    STATE_MIGRATION_STARTED: frozenset(
        {"migration_id", "from_version", "to_version", "backup_ref", "outcome"}
    ),
    STATE_MIGRATION_SUCCEEDED: frozenset(
        {"migration_id", "from_version", "to_version", "backup_ref", "outcome"}
    ),
    STATE_MIGRATION_FAILED: frozenset(
        {"migration_id", "from_version", "to_version", "backup_ref", "error_code"}
    ),
    STATE_REBUILD_STARTED: frozenset({"source_event_range", "candidate_digest", "outcome"}),
    STATE_REBUILD_SUCCEEDED: frozenset({"source_event_range", "candidate_digest", "outcome"}),
    STATE_REBUILD_FAILED: frozenset({"source_event_range", "candidate_digest", "error_code"}),
    LEGACY_UNMAPPED: frozenset(
        {"legacy_type", "legacy_payload_digest", "reason_code", "affected_stage"}
    ),
}

EVENT_TYPES: frozenset[str] = frozenset(REQUIRED_PAYLOAD_FIELDS)

# DR-02: "`checks`, `dependencies`, and `blockers` are arrays of typed
# objects rather than prose." Enforced, because a prose string in one of
# these is exactly the kind of thing that reads fine in a report and
# cannot be acted on by code.
ARRAY_OF_OBJECT_FIELDS: frozenset[str] = frozenset({"checks", "dependencies", "blockers"})

# Payload keys that must never be persisted. DR-02 forbids credentials in
# a payload; a denylist on the key name catches the overwhelmingly common
# way one gets there, which is a config dict being splatted into an event.
FORBIDDEN_PAYLOAD_KEY_SUBSTRINGS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
)


# ── Storage ───────────────────────────────────────────────────────────────────

CANONICAL_EVENT_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS canonical_events (
        event_id         TEXT PRIMARY KEY,
        run_id           TEXT NOT NULL,
        sequence         INTEGER NOT NULL,
        event_type       TEXT NOT NULL,
        event_version    INTEGER NOT NULL,
        occurred_at      TEXT NOT NULL,
        schema_version   INTEGER NOT NULL,
        pipeline_version TEXT NOT NULL,
        scope_id         TEXT,
        stage_id         TEXT,
        attempt          INTEGER,
        correlation_id   TEXT,
        causation_id     TEXT,
        payload          TEXT NOT NULL,
        UNIQUE (run_id, sequence)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_canonical_run ON canonical_events(run_id, sequence)",
    "CREATE INDEX IF NOT EXISTS idx_canonical_type ON canonical_events(event_type)",
)


class EventValidationError(StateError):
    """An event does not satisfy the canonical envelope/payload contract."""

    reason_code = "event_invalid"


class EventSequenceError(StateError):
    """An append would break the per-run sequence rules."""

    reason_code = "event_sequence_invalid"


# ── Envelope ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CanonicalEvent:
    """One persisted event. Immutable, as an append-only record should be."""

    run_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any]
    occurred_at: str
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event_version: int = EVENT_VERSION
    schema_version: int = SCHEMA_VERSION
    pipeline_version: str = PIPELINE_VERSION
    scope_id: str | None = None
    stage_id: str | None = None
    attempt: int | None = None
    correlation_id: str | None = None
    causation_id: str | None = None

    def payload_json(self) -> str:
        """Deterministic JSON: sorted keys, so the same payload always
        produces the same bytes and therefore the same digest."""
        return json.dumps(self.payload, sort_keys=True, separators=(",", ":"))


def new_event(
    run_id: str,
    sequence: int,
    event_type: str,
    payload: dict[str, Any],
    *,
    occurred_at: str | None = None,
    **envelope: Any,
) -> CanonicalEvent:
    """Build and validate an event. Raises rather than returning something
    invalid -- an unvalidated event must never exist as a value."""
    event = CanonicalEvent(
        run_id=run_id,
        sequence=sequence,
        event_type=event_type,
        payload=payload,
        occurred_at=occurred_at if occurred_at is not None else utc_now_iso(),
        **envelope,
    )
    validate_event(event)
    return event


def validate_event(event: CanonicalEvent) -> None:
    """Raise EventValidationError unless *event* satisfies the contract."""
    if event.event_type not in EVENT_TYPES:
        raise EventValidationError(
            f"{event.event_type!r} is not in the closed canonical vocabulary",
            event_type=event.event_type,
            known_types=sorted(EVENT_TYPES),
        )
    if not isinstance(event.payload, dict):
        raise EventValidationError(
            f"payload must be a JSON object, got {type(event.payload).__name__}",
            event_type=event.event_type,
        )
    if event.sequence < 0:
        raise EventValidationError(
            f"sequence must be non-negative, got {event.sequence}", event_type=event.event_type
        )
    if not event.run_id:
        raise EventValidationError("run_id is required", event_type=event.event_type)

    required = REQUIRED_PAYLOAD_FIELDS[event.event_type]
    missing = sorted(required - set(event.payload))
    if missing:
        raise EventValidationError(
            f"{event.event_type} payload is missing required field(s): {', '.join(missing)}",
            event_type=event.event_type,
            missing=missing,
        )

    for name in ARRAY_OF_OBJECT_FIELDS & set(event.payload):
        value = event.payload[name]
        if not isinstance(value, list):
            raise EventValidationError(
                f"{event.event_type} payload field {name!r} must be an array of objects, "
                f"got {type(value).__name__}",
                event_type=event.event_type,
                field=name,
            )
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise EventValidationError(
                    f"{event.event_type} payload field {name!r}[{index}] must be an object, "
                    f"got {type(item).__name__} -- typed objects, not prose",
                    event_type=event.event_type,
                    field=name,
                )

    for key in _walk_keys(event.payload):
        lowered = key.lower()
        for forbidden in FORBIDDEN_PAYLOAD_KEY_SUBSTRINGS:
            if forbidden in lowered:
                raise EventValidationError(
                    f"{event.event_type} payload key {key!r} looks credential-bearing "
                    f"(matched {forbidden!r}); events must never persist credentials",
                    event_type=event.event_type,
                    offending_key=key,
                )

    try:
        json.dumps(event.payload, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise EventValidationError(
            f"{event.event_type} payload is not JSON-serialisable: {exc}",
            event_type=event.event_type,
        ) from exc


def _walk_keys(obj: Any) -> list[str]:
    """Every mapping key anywhere in a nested payload."""
    keys: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            keys.append(str(key))
            keys.extend(_walk_keys(value))
    elif isinstance(obj, list):
        for item in obj:
            keys.extend(_walk_keys(item))
    return keys


# ── Append and read ───────────────────────────────────────────────────────────


def append_event(conn: sqlite3.Connection, event: CanonicalEvent) -> bool:
    """
    Append *event*. Returns True when it was written, False when it was
    already present (idempotent replay of the same `event_id`).

    Three rules, all enforced against the database rather than assumed:

    * The same `event_id` twice is a no-op, not an error. Replay and retry
      must be safe.
    * A *different* event claiming an existing `(run_id, sequence)` is a
      hard error. Two different events cannot occupy one slot.
    * `sequence` strictly increases within a run. Appending at or below
      the run's high-water mark would make the ordering ambiguous, and an
      ambiguous order projects to an ambiguous state.
    """
    validate_event(event)

    existing = conn.execute(
        "SELECT event_type, run_id, sequence FROM canonical_events WHERE event_id = ?",
        (event.event_id,),
    ).fetchone()
    if existing is not None:
        if (
            existing["run_id"] != event.run_id
            or int(existing["sequence"]) != event.sequence
            or existing["event_type"] != event.event_type
        ):
            raise EventSequenceError(
                f"event_id {event.event_id} already exists with different content",
                event_id=event.event_id,
            )
        return False

    slot = conn.execute(
        "SELECT event_id FROM canonical_events WHERE run_id = ? AND sequence = ?",
        (event.run_id, event.sequence),
    ).fetchone()
    if slot is not None:
        raise EventSequenceError(
            f"run {event.run_id} already has an event at sequence {event.sequence} "
            f"({slot['event_id']}); sequences are unique within a run",
            run_id=event.run_id,
            sequence=event.sequence,
        )

    high_water = conn.execute(
        "SELECT MAX(sequence) FROM canonical_events WHERE run_id = ?", (event.run_id,)
    ).fetchone()[0]
    if high_water is not None and event.sequence <= int(high_water):
        raise EventSequenceError(
            f"run {event.run_id} is at sequence {high_water}; refusing to append "
            f"{event.sequence} out of order",
            run_id=event.run_id,
            sequence=event.sequence,
            high_water=int(high_water),
        )

    conn.execute(
        """
        INSERT INTO canonical_events
            (event_id, run_id, sequence, event_type, event_version, occurred_at,
             schema_version, pipeline_version, scope_id, stage_id, attempt,
             correlation_id, causation_id, payload)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.event_id,
            event.run_id,
            event.sequence,
            event.event_type,
            event.event_version,
            event.occurred_at,
            event.schema_version,
            event.pipeline_version,
            event.scope_id,
            event.stage_id,
            event.attempt,
            event.correlation_id,
            event.causation_id,
            event.payload_json(),
        ),
    )
    return True


def read_events(conn: sqlite3.Connection, run_id: str | None = None) -> tuple[CanonicalEvent, ...]:
    """Read events in canonical order: by run, then by sequence.

    Never by insertion rowid. Ordering derived from storage order rather
    than from the declared sequence is the kind of thing that agrees with
    itself right up until a rebuild inserts in a different order.
    """
    if run_id is None:
        rows = conn.execute("SELECT * FROM canonical_events ORDER BY run_id, sequence").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM canonical_events WHERE run_id = ? ORDER BY sequence", (run_id,)
        ).fetchall()
    return tuple(_row_to_event(row) for row in rows)


def _row_to_event(row: sqlite3.Row) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=str(row["event_id"]),
        run_id=str(row["run_id"]),
        sequence=int(row["sequence"]),
        event_type=str(row["event_type"]),
        event_version=int(row["event_version"]),
        occurred_at=str(row["occurred_at"]),
        schema_version=int(row["schema_version"]),
        pipeline_version=str(row["pipeline_version"]),
        scope_id=row["scope_id"],
        stage_id=row["stage_id"],
        attempt=row["attempt"],
        correlation_id=row["correlation_id"],
        causation_id=row["causation_id"],
        payload=json.loads(row["payload"]),
    )


# ── Legacy adapter ────────────────────────────────────────────────────────────

# The only legacy types whose meaning survives translation. Everything
# else in the 43-type legacy vocabulary is per-file content evidence with
# a lossy payload -- see this module's docstring.
LEGACY_LIFECYCLE_TYPES: frozenset[str] = frozenset({"RUN_START", "RUN_END", "STAGE_COMPLETE"})

UNMAPPABLE_REASON_LOSSY = "legacy_payload_lossy"
UNMAPPABLE_REASON_UNKNOWN = "legacy_type_unknown"


def legacy_payload_digest(row: sqlite3.Row) -> str:
    """Stable digest of a legacy row's evidence, so an unmappable event is
    preserved by reference even though its content cannot be trusted."""
    material = json.dumps(
        {
            "event_type": row["event_type"],
            "file_path": row["file_path"],
            "old_value": row["old_value"],
            "new_value": row["new_value"],
            "stage": row["stage"],
            "note": row["note"],
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def adapt_legacy_event(row: sqlite3.Row, sequence: int) -> CanonicalEvent:
    """
    Translate one legacy `events` row into a canonical event.

    A row whose meaning cannot be reconstructed becomes `legacy.unmapped`
    -- never a guessed success. The projector treats any unmapped event in
    a run as a hard block on that run's resume and rebuild.
    """
    legacy_type = str(row["event_type"])
    occurred_at = str(row["ts"]) if row["ts"] is not None else utc_now_iso()
    run_id = str(row["run_id"])

    if legacy_type == "RUN_START":
        return new_event(
            run_id,
            sequence,
            RUN_CREATED,
            {
                "mode": "legacy",
                "config_digest": None,
                "scope_summary": {"source": "legacy_events"},
                "authority": "unknown",
            },
            occurred_at=occurred_at,
        )
    if legacy_type == "RUN_END":
        return new_event(
            run_id,
            sequence,
            RUN_TERMINAL,
            {
                "status": "succeeded",
                "exit_code": 0,
                "reason_code": "legacy_run_end",
                "stage_counts": {},
                "checkpoint_id": None,
                "rollback_status": None,
            },
            occurred_at=occurred_at,
        )
    if legacy_type == "STAGE_COMPLETE":
        stage_id = str(row["stage"]) if row["stage"] is not None else "unknown"
        return new_event(
            run_id,
            sequence,
            STAGE_SUCCEEDED,
            {
                "stage_id": stage_id,
                "attempt": 1,
                "input_digest": None,
                "output_digest": None,
                "counts": {},
            },
            occurred_at=occurred_at,
            stage_id=stage_id,
            attempt=1,
        )

    reason = (
        UNMAPPABLE_REASON_LOSSY
        if legacy_type in KNOWN_LOSSY_LEGACY_TYPES
        else UNMAPPABLE_REASON_UNKNOWN
    )
    return new_event(
        run_id,
        sequence,
        LEGACY_UNMAPPED,
        {
            "legacy_type": legacy_type,
            "legacy_payload_digest": legacy_payload_digest(row),
            "reason_code": reason,
            "affected_stage": row["stage"],
        },
        occurred_at=occurred_at,
        stage_id=row["stage"],
    )


# The 40 content event types this codebase emits, recorded so an
# unmappable event can say *why* it is unmappable: a known-lossy type is a
# different situation from a type nobody has ever seen, and an operator
# reading a blocked rebuild deserves to know which.
KNOWN_LOSSY_LEGACY_TYPES: frozenset[str] = frozenset(
    {
        "ACOUSTIC_DUPE_FOUND",
        "ACOUSTIC_MATCHED",
        "ARTIST_CONSOLIDATED",
        "ART_EMBEDDED",
        "BITROT_DETECTED",
        "BPM_ANALYZED",
        "BPM_SKIPPED_MULTICHANNEL",
        "BPM_SKIPPED_TOO_LONG",
        "CANONICALIZE",
        "CANONICALIZE_DB_COLLISION",
        "CANONICALIZE_VERIFY_FAILED",
        "CORRUPT_DETECTED",
        "CROSS_BATCH_DUPLICATE_FOUND",
        "CURATOR_COPY",
        "DUPE_MOVED_FOR_REVIEW",
        "DUPLICATE_FOUND",
        "FINALIZE_DB_COLLISION",
        "FINALIZE_MOVE",
        "FORGE_TAG",
        "GENRE_ENRICHED",
        "GENRE_FILLED",
        "GHOST_FOUND",
        "HASH_COMPUTED",
        "HASH_FAILED",
        "INGEST",
        "INTEGRITY_FAIL",
        "MB_ARTIST_FOUND",
        "MB_ARTIST_NOT_FOUND",
        "METADATA_EXTRACTED",
        "NEAR_DUPLICATE_FOUND",
        "NORMALIZE_ALBUM",
        "NORMALIZE_ARTIST",
        "NORMALIZE_TITLE",
        "PERMISSION_FIXED",
        "PLAYLIST_WRITTEN",
        "REVIEW_APPLIED",
        "TAGGER_WRITE",
        "TRANSCODE_DONE",
        "TRIBUTE_QUARANTINED",
        "VARIOUS_ARTISTS_FIXED",
    }
)
