"""
MUSAEUS — the quarantine-first mutation boundary and rollback (P0-13)

Every content change a run makes goes through one object. That object
holds the verified checkpoint and the operation journal, refuses to act
after cancellation has been observed, checks each item's precondition
before touching it, and records what it did. Nothing else is allowed to
write to managed content -- which is the whole point, because a mutation
that bypasses the boundary is a mutation the rollback cannot undo and
does not know happened.

**Quarantine first.** A removal or replacement moves the existing bytes
into the checkpoint's quarantine area before the new state is written.
Combined with the checkpoint copy this is deliberately redundant: the
checkpoint proves what the tree looked like, the quarantine holds the
specific thing that was displaced. Redundancy is the correct amount of
paranoia for the one operation whose failure is unrecoverable.

**Rollback never deletes.** Undoing a file the run created cannot mean
removing it -- MCR-003 forbids permanent deletion in P0 -- so rollback
quarantines it instead. The tree ends up as it started; the material that
was in the way ends up somewhere retrievable rather than gone.

**Rollback refuses unexpected overwrites.** Before restoring an item, the
boundary checks that the item still holds what the journal says the run
left there. If something else has changed it since, restoring would
destroy that change, and a rollback that causes data loss is not a
recovery. It stops, reports, and preserves everything.

**Precondition digests.** Each operation records the digest it expected
to find. That is what makes "a concurrent process modified this file
underneath us" a detectable event rather than an outcome nobody notices
until the counts look wrong -- which is roughly the shape of the
2026-08-15 incident.

Scope: this module provides the checked capability and proves it on
fixtures. Migrating the existing thirty-odd stages off their direct
filesystem calls and onto it is integration work that has to happen with
a quiet vault and a review, and is not done here.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from musaeus.safety.manifest import (
    KIND_TAGGED_AUDIO,
    decode_tag_values,
    item_ref_for,
    sha256_file,
)
from musaeus.safety.recovery import (
    OP_ARTWORK_WRITE,
    OP_DATABASE_WRITE,
    OP_MOVE,
    OP_QUARANTINE,
    OP_REPLACE,
    OP_TAG_WRITE,
    STATUS_APPLIED,
    STATUS_FAILED,
    STATUS_RESTORED,
    Checkpoint,
    CollisionError,
    OperationJournal,
    QuarantineRecord,
    quarantine_item,
)
from musaeus.state.cancellation import CancellationGate
from musaeus.state.schema import StateError, utc_now_iso

ROLLBACK_COMPLETED = "completed"
ROLLBACK_FAILED = "failed"


class PreconditionError(StateError):
    """The item is not in the state the operation expected to find it in."""

    reason_code = "precondition_mismatch"


class RollbackFailedError(StateError):
    """Rollback could not restore everything. The run stays failed and all
    recovery material is preserved."""

    reason_code = "rollback_failed"


class UnmanagedPathError(StateError):
    """A path outside the checkpoint's coverage. Refused: an item the
    checkpoint does not cover is an item the rollback cannot restore."""

    reason_code = "path_not_covered"


@dataclass(frozen=True)
class RollbackResult:
    checkpoint_id: str
    outcome: str
    restored: tuple[str, ...] = ()
    already_restored: tuple[str, ...] = ()
    failures: tuple[dict[str, Any], ...] = ()
    remaining_operations: int = 0

    def as_event_payload(self) -> dict[str, Any]:
        """A valid `rollback.completed` payload."""
        return {
            "checkpoint_id": self.checkpoint_id,
            "outcome": self.outcome,
            "remaining_operations": self.remaining_operations,
        }


class MutationBoundary:
    """
    The only sanctioned route to changing managed content.

    Constructed with a *verified* checkpoint -- an unverified one is
    refused, because granting mutation capability against a checkpoint
    nobody validated is granting it against nothing.
    """

    def __init__(
        self,
        checkpoint: Checkpoint,
        journal: OperationJournal,
        *,
        run_id: str,
        source_root: Path,
        gate: CancellationGate | None = None,
    ) -> None:
        if not checkpoint.verified:
            raise StateError(
                f"checkpoint {checkpoint.checkpoint_id} is not verified; refusing to grant "
                f"mutation capability against it"
            )
        self.checkpoint = checkpoint
        self.journal = journal
        self.run_id = run_id
        self.source_root = source_root
        self.gate = gate
        self._quarantines: dict[str, QuarantineRecord] = {}

    # ── Internal helpers ──────────────────────────────────────────────────

    def _relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.source_root))
        except ValueError as exc:
            raise UnmanagedPathError(
                f"{path} lies outside the checkpointed root {self.source_root}",
                path=str(path),
                source_root=str(self.source_root),
            ) from exc

    def _guard(self) -> None:
        if self.gate is not None:
            self.gate.guard_mutation()

    def _record_mutation(self) -> None:
        if self.gate is not None:
            self.gate.record_mutation()

    def _expected_digest(self, item_ref: str) -> str | None:
        """What this item should currently hold, per the record.

        The most recent digest THIS RUN left there, and only the
        checkpoint's digest if the run has not touched it yet. Comparing
        against the checkpoint forever would mean a file could be mutated
        exactly once -- which a real pipeline breaks immediately, since it
        tags a file and then moves it. Found by the reverse-order rollback
        test.

        Read from the journal rather than from memory so the answer
        survives a restart: the journal is the durable record, and an
        in-memory expectation would quietly reset to "the checkpoint" for
        a resumed run, re-introducing the same bug in a harder-to-see
        form.
        """
        for entry in reversed(self.journal.entries()):
            if entry.item_ref == item_ref and entry.status == STATUS_APPLIED:
                return entry.result_digest
        try:
            return self.checkpoint.manifest.entry(item_ref).sha256
        except KeyError:
            return None

    def _check_precondition(self, path: Path, relative: str) -> str | None:
        """Confirm the item still holds what the record says, and return
        its current digest.

        A file that has changed underneath the run is a file whose restore
        target is no longer what was recorded. Continuing would mean the
        rollback silently reverts someone else's work."""
        if not path.exists():
            return None
        current = sha256_file(path)
        expected = self._expected_digest(item_ref_for(relative))
        if expected is not None and expected != current:
            raise PreconditionError(
                f"{relative} has changed since it was last recorded "
                f"(expected {expected[:12]}..., found {current[:12]}...); refusing to "
                f"mutate an item the rollback could no longer restore correctly",
                path=str(path),
                expected=expected,
                found=current,
            )
        return current

    # ── Capabilities ──────────────────────────────────────────────────────

    def write_bytes(self, path: Path, data: bytes, *, kind: str = OP_REPLACE) -> str:
        """Replace a file's content, quarantining the previous bytes first."""
        self._guard()
        relative = self._relative(path)
        before = self._check_precondition(path, relative)

        quarantine_ref = None
        if path.exists():
            record = quarantine_item(
                path, self.checkpoint, reason=f"{kind} by {self.run_id}", run_id=self.run_id
            )
            self._quarantines[record.quarantine_ref] = record
            quarantine_ref = record.quarantine_ref

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        after = sha256_file(path)

        self._record_mutation()
        entry = self.journal.append(
            operation_kind=kind,
            item_ref=item_ref_for(relative),
            precondition_digest=before,
            result_digest=after,
            quarantine_ref=quarantine_ref,
            detail={"relative_path": relative},
        )
        return entry.operation_id

    def fixture_write_tags(self, path: Path, tags: dict[str, str]) -> str:
        """FIXTURE ONLY — this does NOT write a readable tag. Never call it
        on real audio.

        It APPENDS `\n#TAGS {json}` to the file's bytes. On a real ALAC that
        is silent corruption wearing a helpful name: measured 2026-08-26 on
        an encoded .m4a, the file still parses as MP4 (trailing bytes are
        ignored), grows by the appended length, and carries none of the tags
        you asked for -- mutagen reports only the encoder atom. It then
        returns a journal operation id, reporting success.

        Renamed from `write_tags` because that is exactly the name a careful
        person reaches for when they want a journalled tag write, and one
        did: a reviewer on 2026-08-26 nearly recommended wiring
        IdentityTagStage to it before reading the body. There is currently
        NO production-safe journalled tag-write primitive; if you need one,
        build it on mutagen (see musaeus/identity_tags.py, which verifies by
        reading back off disk) rather than on this.

        Exists so the boundary's rollback ordering can be exercised against
        the fake payloads fixtures use, without depending on mutagen's
        behaviour for them. The recorded operation kind is the point."""
        self._guard()
        relative = self._relative(path)
        before = self._check_precondition(path, relative)
        record = quarantine_item(
            path, self.checkpoint, reason=f"tag write by {self.run_id}", run_id=self.run_id
        )
        self._quarantines[record.quarantine_ref] = record

        original = Path(record.quarantine_path).read_bytes()
        path.write_bytes(original + b"\n#TAGS " + json.dumps(tags, sort_keys=True).encode())
        after = sha256_file(path)

        self._record_mutation()
        entry = self.journal.append(
            operation_kind=OP_TAG_WRITE,
            item_ref=item_ref_for(relative),
            precondition_digest=before,
            result_digest=after,
            quarantine_ref=record.quarantine_ref,
            detail={"relative_path": relative, "tags": tags},
        )
        return entry.operation_id

    def fixture_write_artwork(self, path: Path, artwork: bytes) -> str:
        """FIXTURE ONLY — appends `\n#ART <bytes>`, writes no real artwork.

        Same hazard as fixture_write_tags above, and until this rename it
        did not even carry that one's warning docstring."""
        self._guard()
        relative = self._relative(path)
        before = self._check_precondition(path, relative)
        record = quarantine_item(
            path, self.checkpoint, reason=f"artwork write by {self.run_id}", run_id=self.run_id
        )
        self._quarantines[record.quarantine_ref] = record

        original = Path(record.quarantine_path).read_bytes()
        path.write_bytes(original + b"\n#ART " + artwork)
        after = sha256_file(path)

        self._record_mutation()
        entry = self.journal.append(
            operation_kind=OP_ARTWORK_WRITE,
            item_ref=item_ref_for(relative),
            precondition_digest=before,
            result_digest=after,
            quarantine_ref=record.quarantine_ref,
            detail={"relative_path": relative},
        )
        return entry.operation_id

    def move(self, source: Path, destination: Path, *, release_source: bool = True) -> str:
        """Move an item, copy-first, refusing to land on occupied ground.

        Deliberately NOT shutil.move. This is FinalizeStage's sequence,
        adopted here because it is strictly safer and the boundary should
        not be the weaker of the two:

            copy -> verify the copy's size -> atomic same-directory rename
            -> only then release the source

        shutil.move across a filesystem boundary is copy-then-delete with
        no verification in between, so an interrupted move can destroy the
        source having written a short destination. Here, if anything fails
        before the rename, only a temp file is removed and the source is
        untouched.

        `release_source=False` leaves the source in place and returns with
        the destination written. The caller then does whatever else must
        succeed -- for finalize, the archive row UPDATE that can still hit
        a UNIQUE collision -- and calls release_source() once it has. That
        keeps a full, verified copy of the file on disk across the one
        window where the operation can still fail.
        """
        self._guard()
        source_rel = self._relative(source)
        destination_rel = self._relative(destination)
        before = self._check_precondition(source, source_rel)
        if destination.exists():
            raise CollisionError(
                f"move destination {destination} is occupied; refusing to overwrite",
                destination=str(destination),
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        staged = destination.with_name(destination.name + ".mutation_tmp")
        try:
            shutil.copy2(str(source), str(staged))
            src_size, copy_size = source.stat().st_size, staged.stat().st_size
            if src_size != copy_size:
                raise CollisionError(
                    f"size mismatch after copy: source={src_size} bytes, copy={copy_size}",
                    source=str(source),
                )
            staged.rename(destination)  # same parent -> atomic
        except Exception:
            staged.unlink(missing_ok=True)
            raise

        self._record_mutation()
        entry = self.journal.append(
            operation_kind=OP_MOVE,
            item_ref=item_ref_for(source_rel),
            precondition_digest=before,
            result_digest=sha256_file(destination),
            detail={
                "relative_path": source_rel,
                "moved_to": destination_rel,
                "source_released": release_source,
            },
        )
        if release_source:
            self.release_source(entry.operation_id, source)
        return entry.operation_id

    def release_source(self, operation_id: str, source: Path) -> None:
        """Remove the source of a completed move, and record that it went.

        Split out so a caller can keep the original until everything that
        can still fail has succeeded. Journalled as its own entry: a move
        whose source is still present is recoverable by deleting the
        destination, and one whose source is gone is not, so which of the
        two happened has to be on the record rather than inferred.
        """
        try:
            source.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self.journal.append(
                operation_kind=OP_MOVE,
                item_ref=item_ref_for(self._relative(source)),
                status=STATUS_FAILED,
                operation_id=operation_id,
                detail={"source_release_failed": str(exc)},
            )
            raise
        self.journal.append(
            operation_kind=OP_MOVE,
            item_ref=item_ref_for(self._relative(source)),
            operation_id=operation_id,
            detail={"source_released": True},
        )

    def quarantine(self, path: Path, *, reason: str) -> str:
        self._guard()
        relative = self._relative(path)
        before = self._check_precondition(path, relative)
        record = quarantine_item(path, self.checkpoint, reason=reason, run_id=self.run_id)
        self._quarantines[record.quarantine_ref] = record

        self._record_mutation()
        entry = self.journal.append(
            operation_kind=OP_QUARANTINE,
            item_ref=item_ref_for(relative),
            precondition_digest=before,
            result_digest=None,
            quarantine_ref=record.quarantine_ref,
            detail={"relative_path": relative, "reason": reason},
        )
        return entry.operation_id

    def record_database_write(self, description: str) -> str:
        """Journal a database change so rollback restores the checkpointed
        copy. The database's own transactional rollback covers a single
        statement; this covers the case where the filesystem and the
        database have to be undone together."""
        self._guard()
        self._record_mutation()
        entry = self.journal.append(
            operation_kind=OP_DATABASE_WRITE,
            item_ref="::database::",
            detail={"description": description},
        )
        return entry.operation_id

    # ── Rollback ──────────────────────────────────────────────────────────

    def rollback(
        self, *, database_path: Path | None = None, now: str | None = None
    ) -> RollbackResult:
        """
        Undo every applied operation, most recent first.

        Reverse order is dependency order for a linear sequence: a tag
        write on a file that was moved must be undone before the move, or
        the restore lands at a path that no longer holds the file.

        Idempotent. Collision-safe. Never deletes -- material that has to
        be cleared out of the way is quarantined, not removed.
        """
        timestamp = now if now is not None else utc_now_iso()
        restored: list[str] = []
        already: list[str] = []
        failures: list[dict[str, Any]] = []

        applied = [e for e in self.journal.entries() if e.status == STATUS_APPLIED]
        superseded = {e.operation_id for e in self.journal.entries() if e.status == STATUS_RESTORED}
        # One operation can produce several journal entries -- a move writes
        # a second when its source is released. Undo each OPERATION once,
        # using the entry that carries the paths; the release record is
        # bookkeeping, not a separate thing to reverse.
        pending: list = []
        _seen: set[str] = set()
        for _e in applied:
            if _e.operation_id in superseded or _e.operation_id in _seen:
                continue
            _seen.add(_e.operation_id)
            pending.append(
                max(
                    (x for x in applied if x.operation_id == _e.operation_id),
                    key=lambda x: len(x.detail or {}),
                )
            )

        for entry in reversed(pending):
            try:
                if entry.operation_kind == OP_DATABASE_WRITE:
                    self._restore_database(database_path)
                    restored.append(entry.operation_id)
                elif entry.operation_kind == OP_MOVE:
                    self._undo_move(entry)
                    restored.append(entry.operation_id)
                else:
                    self._restore_item(entry)
                    restored.append(entry.operation_id)
                self.journal.mark(entry.operation_id, STATUS_RESTORED, now=timestamp)
            except CollisionError as exc:
                failures.append(
                    {
                        "operation_id": entry.operation_id,
                        "operation_kind": entry.operation_kind,
                        "reason_code": exc.reason_code,
                        "message": str(exc),
                    }
                )

        outcome = ROLLBACK_FAILED if failures else ROLLBACK_COMPLETED
        result = RollbackResult(
            checkpoint_id=self.checkpoint.checkpoint_id,
            outcome=outcome,
            restored=tuple(restored),
            already_restored=tuple(already),
            failures=tuple(failures),
            remaining_operations=len(failures),
        )
        if failures:
            raise RollbackFailedError(
                f"rollback of checkpoint {self.checkpoint.checkpoint_id} could not restore "
                f"{len(failures)} operation(s); all recovery material is preserved and no "
                f"further mutation may proceed",
                checkpoint_id=self.checkpoint.checkpoint_id,
                failures=failures,
                result=result,
            )
        return result

    def _restore_tags(self, target: Path, tags: dict) -> None:
        """Put the checkpointed tag values back on a file whose bytes were
        never copied. This is what makes a 468 GB library rollback-able for
        ForgeStage and TaggerStage, which change tags and nothing else."""
        import mutagen  # type: ignore[import-untyped]

        audio = mutagen.File(str(target))
        if audio is None:
            raise CollisionError(f"{target} cannot be opened to restore its tags", path=str(target))
        if audio.tags is None:
            audio.add_tags()
        for key in [k for k in audio.tags if not str(k).startswith("covr")]:
            del audio.tags[key]
        for key, values in tags.items():
            audio.tags[key] = decode_tag_values(values)
        audio.save()

    def _restore_item(self, entry: Any) -> None:
        relative = entry.detail.get("relative_path")
        if relative is None:
            return
        target = self.source_root / relative

        # A tag-captured entry has no copied bytes to put back -- its
        # restorable state is the tag values in the manifest.
        try:
            manifest_entry = self.checkpoint.manifest.entry(item_ref_for(relative))
        except KeyError:
            manifest_entry = None
        if manifest_entry is not None and manifest_entry.kind == KIND_TAGGED_AUDIO:
            if manifest_entry.tags is None:
                raise CollisionError(
                    f"{relative} was tag-captured but no tags were recorded; it cannot be restored",
                    relative_path=relative,
                )
            if target.exists():
                self._restore_tags(target, manifest_entry.tags)
            return

        checkpointed = self.checkpoint.payload_root / relative

        if target.exists():
            current = sha256_file(target)
            if entry.result_digest is not None and current != entry.result_digest:
                raise CollisionError(
                    f"{relative} no longer holds what this run left there "
                    f"(expected {entry.result_digest[:12]}..., found {current[:12]}...); "
                    f"restoring would destroy a change made since",
                    relative_path=relative,
                )
            if checkpointed.is_file() and current == sha256_file(checkpointed):
                return  # already back to the checkpointed state
        if not checkpointed.is_file():
            # Nothing checkpointed means the run created this item; clear it
            # out of the way rather than deleting it.
            if target.exists():
                record = quarantine_item(
                    target,
                    self.checkpoint,
                    reason=f"rolled back creation by {self.run_id}",
                    run_id=self.run_id,
                )
                self._quarantines[record.quarantine_ref] = record
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(checkpointed, target)

    def _undo_move(self, entry: Any) -> None:
        relative = (entry.detail or {}).get("relative_path")
        moved_to = (entry.detail or {}).get("moved_to")
        if not relative or not moved_to:
            return  # release-of-source record; the move entry holds the paths
        origin = self.source_root / relative
        destination = self.source_root / moved_to

        if not destination.exists():
            self._restore_item(entry)
            return
        current = sha256_file(destination)
        if entry.result_digest is not None and current != entry.result_digest:
            raise CollisionError(
                f"{moved_to} no longer holds what this run moved there; restoring would "
                f"destroy a change made since",
                relative_path=moved_to,
            )
        if origin.exists():
            if sha256_file(origin) == current:
                return
            raise CollisionError(
                f"move origin {relative} is occupied by different content; refusing to "
                f"overwrite it during rollback",
                relative_path=relative,
            )
        origin.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(destination), str(origin))

    def _restore_database(self, database_path: Path | None) -> None:
        if database_path is None:
            return
        checkpointed = self.checkpoint.payload_root / "__database__" / database_path.name
        if not checkpointed.is_file():
            raise CollisionError(
                "the checkpoint holds no database copy; cannot restore the database",
                database=str(database_path),
            )
        shutil.copy2(checkpointed, database_path)
