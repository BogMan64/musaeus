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
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from musaeus.safety.manifest import (
    KIND_FILE,
    KIND_TAGGED_AUDIO,
    TAGGED_PREFIX,
    ManifestEntry,
    current_tagged_identity,
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
    restore_quarantined,
)
from musaeus.state.cancellation import CancellationGate
from musaeus.state.schema import StateError, utc_now_iso

ROLLBACK_COMPLETED = "completed"
ROLLBACK_FAILED = "failed"


def _digest_from_disk(path: Path) -> str:
    """SHA-256 of what the disk holds: flush and drop the cached pages first,
    or the check reads back the copy still in memory."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)
    return sha256_file(path)


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
        # The checkpoint may cover less than the boundary: finalize and
        # canonicalize checkpoint STAGING but move files across the vault.
        # It must at least lie inside it, or its record is never consulted.
        checkpoint_root = Path(checkpoint.manifest.source_root)
        if checkpoint_root != source_root and source_root not in checkpoint_root.parents:
            raise StateError(
                f"checkpoint {checkpoint.checkpoint_id} covers {checkpoint_root}, which is not "
                f"inside the boundary's root {source_root}; its record would never be consulted"
            )
        self.checkpoint = checkpoint
        self.journal = journal
        self.run_id = run_id
        self.source_root = source_root
        self.gate = gate
        self._quarantines: dict[str, QuarantineRecord] = {}
        self._checkpoint_root = checkpoint_root
        self._manifest_index = {e.item_ref: e for e in checkpoint.manifest.entries}

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

    def _manifest_entry(self, path: Path) -> ManifestEntry | None:
        """The checkpoint's record of *path*, or None if it holds none.

        Looked up by the path relative to the CHECKPOINT's root, not the
        boundary's. The two differ for finalize and canonicalize (STAGING
        against the vault), and looking up the vault-relative path found
        nothing, so the checkpoint was never consulted (September review, B).
        """
        try:
            relative = str(path.relative_to(self._checkpoint_root))
        except ValueError:
            return None
        return self._manifest_index.get(item_ref_for(relative))

    def _payload_copy(self, path: Path) -> Path | None:
        """The checkpoint's byte copy of *path*, if it made one."""
        entry = self._manifest_entry(path)
        if entry is None or entry.kind != KIND_FILE:
            return None
        copy = self.checkpoint.payload_root / entry.relative_path
        return copy if copy.is_file() else None

    def _guard(self) -> None:
        if self.gate is not None:
            self.gate.guard_mutation()

    def _record_mutation(self) -> None:
        if self.gate is not None:
            self.gate.record_mutation()

    def _expected_digest(self, path: Path, relative: str) -> str | None:
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
        last = self.journal.last_applied(item_ref_for(relative))
        if last is not None:
            return last.result_digest
        entry = self._manifest_entry(path)
        return entry.sha256 if entry is not None else None

    def _check_precondition(self, path: Path, relative: str) -> str | None:
        """Confirm the item still holds what the record says, and return
        its current digest.

        A file that has changed underneath the run is a file whose restore
        target is no longer what was recorded. Continuing would mean the
        rollback silently reverts someone else's work."""
        expected = self._expected_digest(path, relative)
        if not path.exists():
            if expected is not None:
                # Recorded as holding something, now gone: a concurrent
                # removal, not a new file (September review, F3).
                raise PreconditionError(
                    f"{relative} is recorded as present but has vanished; refusing to treat "
                    f"it as a new item",
                    path=str(path),
                    expected=expected,
                )
            return None
        current = sha256_file(path)
        # A tag-captured entry records a tagged identity, not a SHA-256, so
        # compare like with like (September review, A). What is returned,
        # and journalled, is always the SHA-256.
        found = (
            current_tagged_identity(path)
            if expected is not None and expected.startswith(TAGGED_PREFIX)
            else current
        )
        if expected is not None and expected != found:
            raise PreconditionError(
                f"{relative} has changed since it was last recorded "
                f"(expected {expected[:12]}..., found {found[:12]}...); refusing to "
                f"mutate an item the rollback could no longer restore correctly",
                path=str(path),
                expected=expected,
                found=found,
            )
        return current

    def _require_byte_restorable(self, path: Path, relative: str) -> None:
        """Refuse a byte write over a file the rollback could not put back.

        Rollback restores overwritten bytes from the checkpoint's copy. A
        file the checkpoint did not copy -- outside it, or tag-captured --
        and that this run did not create has nothing to restore from.
        """
        if not path.exists():
            return  # a creation; rollback clears it out of the way
        entry = self._manifest_entry(path)
        if entry is not None and entry.kind == KIND_TAGGED_AUDIO:
            raise UnmanagedPathError(
                f"{relative} is tag-captured: the checkpoint holds its tags, not its bytes, "
                f"so a byte write could not be rolled back",
                path=str(path),
            )
        if entry is None and self.journal.last_applied(item_ref_for(relative)) is None:
            raise UnmanagedPathError(
                f"{relative} is not in checkpoint {self.checkpoint.checkpoint_id}; a byte "
                f"write over it could not be rolled back",
                path=str(path),
            )

    # ── Capabilities ──────────────────────────────────────────────────────

    def write_bytes(self, path: Path, data: bytes, *, kind: str = OP_REPLACE) -> str:
        """Replace a file's content, quarantining the previous bytes first."""
        self._guard()
        relative = self._relative(path)
        before = self._check_precondition(path, relative)
        self._require_byte_restorable(path, relative)

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
        self._require_byte_restorable(path, relative)
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
        self._require_byte_restorable(path, relative)
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
            # Content, not just size: a damaged copy of the right size was
            # accepted and the source then released (review of #87, finding
            # 8). Read back from the disk, not from the pages just written.
            copied = _digest_from_disk(staged)
            if before is not None and copied != before:
                raise CollisionError(
                    f"the copy of {source_rel} does not match it "
                    f"({before[:12]}... vs {copied[:12]}...); source kept",
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
            # Enough to put it back from the journal alone, for a rollback
            # run by a fresh boundary that never saw this one's memory.
            detail={
                "relative_path": relative,
                "reason": reason,
                "quarantine_path": record.quarantine_path,
                "quarantined_sha256": record.sha256,
            },
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

        entries = self.journal.entries()
        superseded = {e.operation_id for e in entries if e.status == STATUS_RESTORED}
        # One operation can produce several journal entries -- a move writes
        # a second when its source is released. Undo each OPERATION once,
        # using the entry that carries the paths; the release record is
        # bookkeeping, not a separate thing to reverse.
        chosen: dict[str, Any] = {}
        for e in entries:
            if e.status != STATUS_APPLIED or e.operation_id in superseded:
                continue
            best = chosen.get(e.operation_id)
            if best is None or len(e.detail or {}) > len(best.detail or {}):
                chosen[e.operation_id] = e
        pending = list(chosen.values())

        for entry in reversed(pending):
            try:
                if entry.operation_kind == OP_DATABASE_WRITE:
                    self._restore_database(database_path)
                    restored.append(entry.operation_id)
                elif entry.operation_kind == OP_MOVE:
                    self._undo_move(entry)
                    restored.append(entry.operation_id)
                elif entry.operation_kind == OP_QUARANTINE:
                    self._undo_quarantine(entry)
                    restored.append(entry.operation_id)
                else:
                    self._restore_item(entry)
                    restored.append(entry.operation_id)
                self.journal.mark(entry.operation_id, STATUS_RESTORED, now=timestamp)
            except Exception as exc:
                # Any failure is this operation's, not the rollback's: record
                # it and go on to the rest. Catching only CollisionError let
                # one PermissionError abandon every operation after it
                # (September review, F4).
                failures.append(
                    {
                        "operation_id": entry.operation_id,
                        "operation_kind": entry.operation_kind,
                        "reason_code": getattr(exc, "reason_code", "unexpected_error"),
                        "error_type": type(exc).__name__,
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
        from mutagen._vorbis import VCommentDict  # type: ignore[import-untyped]
        from mutagen.mp4 import MP4Tags  # type: ignore[import-untyped]

        audio = mutagen.File(str(target))
        if audio is None:
            raise CollisionError(f"{target} cannot be opened to restore its tags", path=str(target))
        if audio.tags is None:
            audio.add_tags()
        if not isinstance(audio.tags, (MP4Tags, VCommentDict)):
            # read_tags captures only these; anything else is copied instead.
            raise CollisionError(
                f"{target}: tags of this format cannot be put back from a capture",
                path=str(target),
            )
        # keys(), not iteration: iterating a Vorbis comment yields
        # (key, value) pairs, which crashed the restore (September review, F1).
        for key in [k for k in audio.tags.keys() if not str(k).startswith("covr")]:  # noqa: SIM118
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
        manifest_entry = self._manifest_entry(target)
        if manifest_entry is not None and manifest_entry.kind == KIND_TAGGED_AUDIO:
            if manifest_entry.tags is None:
                raise CollisionError(
                    f"{relative} was tag-captured but no tags were recorded; it cannot be restored",
                    relative_path=relative,
                )
            if not target.exists():
                raise CollisionError(
                    f"{relative} is gone, and the checkpoint holds its tags, not its bytes; "
                    f"it cannot be restored",
                    relative_path=relative,
                )
            self._restore_tags(target, manifest_entry.tags)
            return

        checkpointed = self._payload_copy(target)

        if target.exists():
            current = sha256_file(target)
            if entry.result_digest is not None and current != entry.result_digest:
                raise CollisionError(
                    f"{relative} no longer holds what this run left there "
                    f"(expected {entry.result_digest[:12]}..., found {current[:12]}...); "
                    f"restoring would destroy a change made since",
                    relative_path=relative,
                )
            if checkpointed is not None and current == sha256_file(checkpointed):
                return  # already back to the checkpointed state
        if checkpointed is None:
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
            self._undo_move_without_destination(entry, origin, relative, moved_to)
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
                # The source was kept (finalize releases it only once the
                # archive row lands). The move's effect is the copy: clear it
                # away -- quarantined, rollback never deletes. Returning here
                # left it behind as an untracked file, and a re-run made a
                # "(2)" beside it (review of #87, finding 2).
                record = quarantine_item(
                    destination,
                    self.checkpoint,
                    reason=f"rolled back the copy made by {self.run_id}",
                    run_id=self.run_id,
                )
                self._quarantines[record.quarantine_ref] = record
                return
            raise CollisionError(
                f"move origin {relative} is occupied by different content; refusing to "
                f"overwrite it during rollback",
                relative_path=relative,
            )
        origin.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(destination), str(origin))

    def _undo_move_without_destination(
        self, entry: Any, origin: Path, relative: str, moved_to: str
    ) -> None:
        """The move's copy is already gone -- finalize removes its own copy
        when the archive update collides.

        A move never creates its origin, so this must never take the "run
        created this" route: that route quarantined the origin, which for
        an INBOX passthrough is the only copy.
        """
        if origin.exists():
            if (
                entry.precondition_digest is None
                or sha256_file(origin) == entry.precondition_digest
            ):
                return  # the origin is as it was; nothing left to undo
            raise CollisionError(
                f"{moved_to} is gone and {relative} holds different content than when it was "
                f"moved; leaving it alone",
                relative_path=relative,
            )
        copy = self._payload_copy(origin)
        if copy is None:
            raise CollisionError(
                f"neither {relative} nor {moved_to} exists, and the checkpoint holds no copy "
                f"of {relative}; it cannot be restored",
                relative_path=relative,
            )
        origin.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(copy, origin)

    def _undo_quarantine(self, entry: Any) -> None:
        """Put a quarantined item back, from what the journal recorded.

        Through restore_quarantined, which refuses to overwrite anything
        that has arrived at the path since. Copying the checkpoint over the
        path did overwrite it (September review, F2), and found nothing for
        an item outside the checkpoint, such as canonicalize's INBOX
        originals, which then stayed in quarantine while the rollback
        reported success.
        """
        detail = entry.detail or {}
        relative = detail.get("relative_path")
        if not relative or not entry.quarantine_ref:
            raise CollisionError(
                f"journal entry {entry.operation_id} does not say what it quarantined",
                operation_id=entry.operation_id,
            )
        # Journals written before quarantined_sha256 existed: the
        # precondition digest is the same SHA-256 of the same bytes.
        digest = detail.get("quarantined_sha256") or entry.precondition_digest
        if not digest or str(digest).startswith(TAGGED_PREFIX):
            raise CollisionError(
                f"no content digest recorded for quarantined {relative}; cannot restore it safely",
                relative_path=relative,
            )
        quarantine_path = detail.get("quarantine_path") or str(
            self.checkpoint.quarantine_root / entry.quarantine_ref / Path(str(relative)).name
        )
        restore_quarantined(
            QuarantineRecord(
                quarantine_ref=entry.quarantine_ref,
                source_path=str(self.source_root / str(relative)),
                quarantine_path=str(quarantine_path),
                reason=str(detail.get("reason", "")),
                run_id=self.run_id,
                sha256=str(digest),
                quarantined_at=entry.recorded_at,
            )
        )

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
