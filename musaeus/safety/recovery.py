"""
MUSAEUS — checkpoint, journal and quarantine primitives (P0-12)

Three primitives, in the order a run uses them:

**Checkpoint.** A verified copy of everything a run may touch, written to
the declared disposable recovery target BEFORE any mutation capability is
granted. Verification re-hashes every copied byte against the manifest;
a checkpoint that was written but not verified is not a checkpoint, it is
a hope. Capacity is checked first, against both the fixed 100 GB cap and
the safely usable space actually present.

**Journal.** A durable, ordered, append-only record of what was done, so
a rollback knows what to undo and in what order. Flushed and fsynced per
entry: a journal that loses its last entries in the crash that made it
necessary is worse than none, because it will confidently under-restore.

**Quarantine.** Removals and replacements move; they do not delete. This
module deliberately exposes no delete operation at all -- MCR-003 says
"no item is permanently deleted in P0", and the reliable way to honour
that is not to write the function. A test asserts the public API contains
nothing that deletes.

Collisions are refused, never resolved by overwriting. If a quarantine or
restore destination is already occupied by different content, that is a
fact the operator needs, not an obstacle for the code to route around.
This project has a live example of the alternative: a duplicate cascade
that moved ~11,160 files while every step reported success.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from musaeus.safety.manifest import (
    KIND_DATABASE,
    KIND_TAGGED_AUDIO,
    Manifest,
    build_manifest,
    sha256_file,
)
from musaeus.state.policy import RECOVERY_CAP_BYTES, RECOVERY_CAP_LABEL
from musaeus.state.schema import StateError, utc_now_iso

PAYLOAD_DIRNAME = "payload"
MANIFEST_FILENAME = "manifest.json"
JOURNAL_FILENAME = "journal.jsonl"
QUARANTINE_DIRNAME = "quarantine"

# Operation kinds recorded in the journal. Closed vocabulary.
OP_TAG_WRITE = "tag_write"
OP_ARTWORK_WRITE = "artwork_write"
OP_MOVE = "move"
OP_REPLACE = "replace"
OP_QUARANTINE = "quarantine"
OP_DATABASE_WRITE = "database_write"
OPERATION_KINDS: frozenset[str] = frozenset(
    {OP_TAG_WRITE, OP_ARTWORK_WRITE, OP_MOVE, OP_REPLACE, OP_QUARANTINE, OP_DATABASE_WRITE}
)

STATUS_APPLIED = "applied"
STATUS_RESTORED = "restored"
STATUS_FAILED = "failed"


class CheckpointError(StateError):
    reason_code = "checkpoint_failed"


class CheckpointCapacityError(StateError):
    reason_code = "recovery_capacity_exceeded"


class CollisionError(StateError):
    """A destination is occupied by different content. Refused, not resolved."""

    reason_code = "recovery_collision"


class JournalError(StateError):
    reason_code = "journal_invalid"


# ── Checkpoint ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    root: Path
    manifest: Manifest
    manifest_digest: str
    created_at: str
    verified: bool
    recovery_target: str

    @property
    def payload_root(self) -> Path:
        return self.root / PAYLOAD_DIRNAME

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_FILENAME

    @property
    def quarantine_root(self) -> Path:
        return self.root / QUARANTINE_DIRNAME

    def coverage(self) -> dict[str, int]:
        return self.manifest.coverage()

    def as_event_payload(self) -> dict[str, object]:
        """A valid `checkpoint.created` / `checkpoint.verified` payload."""
        return {
            "checkpoint_id": self.checkpoint_id,
            "manifest_digest": self.manifest_digest,
            "coverage": self.coverage(),
            "recovery_target": self.recovery_target,
        }


def _safely_usable_bytes(path: Path, reserve_bytes: int, reserve_fraction: float) -> int:
    usage = shutil.disk_usage(str(path))
    reserve = max(reserve_bytes, int(usage.total * reserve_fraction))
    return max(0, usage.free - reserve)


def assert_capacity(
    required_bytes: int,
    recovery_root: Path,
    *,
    reserve_bytes: int = 2 * 10**9,
    reserve_fraction: float = 0.05,
) -> int:
    """
    Raise unless *required_bytes* fits under the fixed cap AND within the
    safely usable space actually present. Returns the usable figure.

    Both limits, not either: the cap is policy and the free space is
    physics, and passing one says nothing about the other.
    """
    if required_bytes > RECOVERY_CAP_BYTES:
        raise CheckpointCapacityError(
            f"checkpoint would need {required_bytes} bytes, over the fixed "
            f"{RECOVERY_CAP_LABEL} cap",
            required=required_bytes,
            cap=RECOVERY_CAP_BYTES,
            remediation="reduce the scope of this run",
        )
    usable = _safely_usable_bytes(recovery_root, reserve_bytes, reserve_fraction)
    if required_bytes > usable:
        raise CheckpointCapacityError(
            f"checkpoint would need {required_bytes} bytes; the recovery target has "
            f"{usable} safely usable",
            required=required_bytes,
            usable=usable,
            remediation="free space on the recovery target or reduce the scope",
        )
    return usable


def create_checkpoint(
    source_root: Path,
    recovery_root: Path,
    *,
    checkpoint_id: str | None = None,
    database_path: Path | None = None,
    now: str | None = None,
    reserve_bytes: int = 2 * 10**9,
    reserve_fraction: float = 0.05,
    capture_tags: bool = False,
) -> Checkpoint:
    """
    Copy *source_root* into the recovery target, manifest it, and verify.

    Order is the contract: manifest first (so the record describes the
    pre-copy truth), capacity check second (so an impossible checkpoint
    fails before writing anything), copy third, verify last. Returning an
    unverified Checkpoint is not possible -- verification failure raises.
    """
    from musaeus.state.migrator import _assert_usable_recovery_root

    _assert_usable_recovery_root(recovery_root)

    identifier = checkpoint_id or f"ckpt_{uuid.uuid4().hex[:12]}"
    timestamp = now if now is not None else utc_now_iso()

    manifest = build_manifest(
        source_root,
        checkpoint_id=identifier,
        created_at=timestamp,
        database_path=database_path,
        capture_tags=capture_tags,
    )
    # Tag-captured audio contributes no payload bytes -- its restorable
    # state is the tag values held in the manifest, not a copy of the file.
    # Counting its size here would make a 483 GB library look impossible to
    # checkpoint when the thing being checkpointed is 15 MB of tags.
    assert_capacity(
        manifest.payload_bytes,
        recovery_root,
        reserve_bytes=reserve_bytes,
        reserve_fraction=reserve_fraction,
    )

    root = recovery_root / identifier
    if root.exists():
        raise CollisionError(
            f"checkpoint directory {root} already exists; refusing to write over it",
            checkpoint_id=identifier,
        )
    payload = root / PAYLOAD_DIRNAME
    payload.mkdir(parents=True)
    (root / QUARANTINE_DIRNAME).mkdir()

    for entry in manifest.entries:
        if entry.kind == KIND_TAGGED_AUDIO:
            continue  # tags live in the manifest; the bytes are not copied
        if entry.kind == KIND_DATABASE:
            assert database_path is not None
            destination = payload / "__database__" / database_path.name
        else:
            destination = payload / entry.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if entry.kind == KIND_DATABASE:
            # Narrowed for the type checker as well as the reader: a
            # database entry only exists because database_path was given.
            assert database_path is not None
            source = database_path
        else:
            source = source_root / entry.relative_path
        shutil.copy2(source, destination)

    (root / MANIFEST_FILENAME).write_text(manifest.to_json())

    checkpoint = Checkpoint(
        checkpoint_id=identifier,
        root=root,
        manifest=manifest,
        manifest_digest=manifest.digest,
        created_at=timestamp,
        verified=False,
        recovery_target=str(recovery_root),
    )
    verify_checkpoint(checkpoint)
    return replace(checkpoint, verified=True)


def verify_checkpoint(checkpoint: Checkpoint, *, database_path: Path | None = None) -> None:
    """
    Re-hash every copied byte against the manifest.

    Not a file-count check and not a size check: a truncated or partially
    written copy has a plausible size and the wrong content, and this is
    the only step standing between "we have a backup" and "we believed we
    had a backup".
    """
    if not checkpoint.manifest_path.is_file():
        raise CheckpointError(
            f"checkpoint {checkpoint.checkpoint_id} has no manifest",
            checkpoint_id=checkpoint.checkpoint_id,
        )
    stored = Manifest.from_json(checkpoint.manifest_path.read_text())
    if stored.digest != checkpoint.manifest_digest:
        raise CheckpointError(
            f"checkpoint {checkpoint.checkpoint_id} manifest digest does not match the "
            f"manifest on disk",
            checkpoint_id=checkpoint.checkpoint_id,
        )

    for entry in stored.entries:
        if entry.kind == KIND_TAGGED_AUDIO:
            # Verify the captured state exists, not a copy that was never made.
            if entry.tags is None:
                raise CheckpointError(
                    f"tag-captured entry {entry.relative_path} has no tags recorded; "
                    f"it is not restorable",
                    checkpoint_id=checkpoint.checkpoint_id,
                    item=entry.relative_path,
                )
            continue
        if entry.kind == KIND_DATABASE:
            name = entry.relative_path.rsplit("::", 1)[-1]
            copied = checkpoint.payload_root / "__database__" / name
        else:
            copied = checkpoint.payload_root / entry.relative_path
        if not copied.is_file():
            raise CheckpointError(
                f"checkpoint is missing {entry.relative_path}",
                checkpoint_id=checkpoint.checkpoint_id,
                missing=entry.relative_path,
            )
        actual = sha256_file(copied)
        if actual != entry.sha256:
            raise CheckpointError(
                f"checkpoint copy of {entry.relative_path} does not match its manifest "
                f"digest (expected {entry.sha256[:12]}..., found {actual[:12]}...)",
                checkpoint_id=checkpoint.checkpoint_id,
                item=entry.relative_path,
            )


# ── Journal ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class JournalEntry:
    sequence: int
    operation_id: str
    operation_kind: str
    item_ref: str
    status: str
    recorded_at: str
    precondition_digest: str | None = None
    result_digest: str | None = None
    quarantine_ref: str | None = None
    detail: dict[str, object] = field(default_factory=dict)


class OperationJournal:
    """
    Durable, ordered, append-only operation record.

    One JSON object per line, flushed and fsynced on every append. The
    fsync is the point: a journal that loses its last entries in the very
    crash that made it necessary will confidently under-restore, which is
    worse than having no journal at all because it looks complete.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def append(
        self,
        *,
        operation_kind: str,
        item_ref: str,
        status: str = STATUS_APPLIED,
        operation_id: str | None = None,
        precondition_digest: str | None = None,
        result_digest: str | None = None,
        quarantine_ref: str | None = None,
        detail: dict[str, object] | None = None,
        now: str | None = None,
    ) -> JournalEntry:
        if operation_kind not in OPERATION_KINDS:
            raise JournalError(
                f"{operation_kind!r} is not a known operation kind; the vocabulary is closed",
                operation_kind=operation_kind,
                known=sorted(OPERATION_KINDS),
            )
        entry = JournalEntry(
            sequence=len(self.entries()),
            operation_id=operation_id or f"op_{uuid.uuid4().hex[:12]}",
            operation_kind=operation_kind,
            item_ref=item_ref,
            status=status,
            recorded_at=now if now is not None else utc_now_iso(),
            precondition_digest=precondition_digest,
            result_digest=result_digest,
            quarantine_ref=quarantine_ref,
            detail=detail or {},
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(entry), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return entry

    def entries(self) -> tuple[JournalEntry, ...]:
        if not self.path.is_file():
            return ()
        rows: list[JournalEntry] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rows.append(JournalEntry(**json.loads(line)))
        return tuple(rows)

    def applied(self) -> tuple[JournalEntry, ...]:
        return tuple(e for e in self.entries() if e.status == STATUS_APPLIED)

    def mark(self, operation_id: str, status: str, *, now: str | None = None) -> JournalEntry:
        """Record a status change as a NEW entry.

        Append-only: the original record of what was applied is never
        edited. A journal you can rewrite is a journal that can be made to
        agree with any story."""
        original = next((e for e in self.entries() if e.operation_id == operation_id), None)
        if original is None:
            raise JournalError(f"unknown operation_id {operation_id}", operation_id=operation_id)
        return self.append(
            operation_kind=original.operation_kind,
            item_ref=original.item_ref,
            status=status,
            operation_id=operation_id,
            precondition_digest=original.precondition_digest,
            result_digest=original.result_digest,
            quarantine_ref=original.quarantine_ref,
            detail={"supersedes_sequence": original.sequence},
            now=now,
        )


# ── Quarantine ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class QuarantineRecord:
    quarantine_ref: str
    source_path: str
    quarantine_path: str
    reason: str
    run_id: str
    sha256: str
    quarantined_at: str


def quarantine_item(
    path: Path,
    checkpoint: Checkpoint,
    *,
    reason: str,
    run_id: str,
    now: str | None = None,
) -> QuarantineRecord:
    """
    Move *path* into the checkpoint's quarantine area.

    A move, never a delete: the bytes exist at the new location when this
    returns, and the record says where. MCR-003 requires source, reason,
    timestamp, run identifier and restoration result to be recorded for
    every quarantine action.

    A destination already occupied by DIFFERENT content raises rather than
    being resolved -- see the module docstring.
    """
    if not path.is_file():
        raise CollisionError(f"cannot quarantine {path}: it is not a file", source_path=str(path))
    digest = sha256_file(path)
    timestamp = now if now is not None else utc_now_iso()
    reference = f"q_{uuid.uuid4().hex[:12]}"

    destination = checkpoint.quarantine_root / reference / path.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if sha256_file(destination) != digest:
            raise CollisionError(
                f"quarantine destination {destination} holds different content; refusing "
                f"to overwrite",
                destination=str(destination),
            )
    else:
        shutil.move(str(path), str(destination))

    return QuarantineRecord(
        quarantine_ref=reference,
        source_path=str(path),
        quarantine_path=str(destination),
        reason=reason,
        run_id=run_id,
        sha256=digest,
        quarantined_at=timestamp,
    )


def restore_quarantined(record: QuarantineRecord) -> None:
    """
    Move a quarantined item back to where it came from.

    Refuses if something different now occupies the original path: putting
    the old file back over a new one would turn a rollback into a second
    act of data loss.
    """
    source = Path(record.quarantine_path)
    target = Path(record.source_path)

    # Already-restored is checked FIRST, before the quarantine copy is
    # looked for. A successful restore consumes the quarantine copy, so
    # asking "is the quarantined item still there?" first makes the second
    # call to an idempotent operation fail on the evidence of the first
    # one having worked. Found by the idempotence test, which is why it is
    # worth writing tests for the boring properties too.
    if target.exists() and sha256_file(target) == record.sha256:
        return

    if not source.is_file():
        raise CollisionError(
            f"quarantined item {source} is missing; cannot restore",
            quarantine_ref=record.quarantine_ref,
        )
    if target.exists():
        raise CollisionError(
            f"{target} now holds different content than the quarantined item; refusing to "
            f"overwrite it during restore",
            quarantine_ref=record.quarantine_ref,
            target=str(target),
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
