"""
P0-13 — quarantine-first mutation boundary and rollback.

MCR-003's acceptance criterion is exact restoration: "restore fixture
files, tags, artwork, database records, and locations to their recorded
pre-run state". So the tests capture the tree before, mutate through the
boundary, roll back, and compare hashes and paths -- not counts.

The failure cases matter as much as the success case. A rollback that
overwrites someone else's later change has not recovered anything, it has
lost data twice.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from musaeus.safety.manifest import sha256_file
from musaeus.safety.mutation import (
    ROLLBACK_COMPLETED,
    MutationBoundary,
    PreconditionError,
    RollbackFailedError,
    UnmanagedPathError,
)
from musaeus.safety.recovery import (
    JOURNAL_FILENAME,
    OP_MOVE,
    OP_TAG_WRITE,
    STATUS_RESTORED,
    CollisionError,
    OperationJournal,
    create_checkpoint,
)
from musaeus.state.cancellation import CancellationGate, MutationAfterCancellationError
from musaeus.state.schema import StateError

NOW = "2026-08-23T22:00:00Z"


def _snapshot(root: Path) -> dict[str, str]:
    """Every file's path and content digest. The comparison target."""
    return {
        str(p.relative_to(root)): sha256_file(p) for p in sorted(root.rglob("*")) if p.is_file()
    }


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "library"
    (root / "Bob Seger").mkdir(parents=True)
    (root / "The Byrds").mkdir(parents=True)
    (root / "Bob Seger" / "Night Moves.m4a").write_bytes(b"PAYLOAD ONE")
    (root / "Bob Seger" / "Hollywood Nights.m4a").write_bytes(b"PAYLOAD TWO")
    (root / "The Byrds" / "Eight Miles High.m4a").write_bytes(b"PAYLOAD THREE")
    return root


@pytest.fixture
def boundary(library, tmp_path):
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    checkpoint = create_checkpoint(library, recovery, checkpoint_id="c1", now=NOW)
    journal = OperationJournal(checkpoint.root / JOURNAL_FILENAME)
    return MutationBoundary(checkpoint, journal, run_id="run-A", source_root=library)


# ── Capability gating ─────────────────────────────────────────────────────────


class TestCapabilityGating:
    def test_an_unverified_checkpoint_cannot_grant_capability(self, library, tmp_path):
        from dataclasses import replace

        recovery = tmp_path / "recovery"
        recovery.mkdir()
        checkpoint = create_checkpoint(library, recovery, checkpoint_id="c1", now=NOW)
        unverified = replace(checkpoint, verified=False)
        journal = OperationJournal(checkpoint.root / JOURNAL_FILENAME)

        with pytest.raises(StateError) as exc:
            MutationBoundary(unverified, journal, run_id="run-A", source_root=library)
        assert "not verified" in str(exc.value)

    def test_a_path_outside_the_checkpoint_is_refused(self, boundary, tmp_path):
        """An item the checkpoint does not cover is an item the rollback
        cannot restore, so it may not be mutated."""
        outsider = tmp_path / "elsewhere.m4a"
        outsider.write_bytes(b"NOT COVERED")
        with pytest.raises(UnmanagedPathError):
            boundary.write_bytes(outsider, b"NEW")

    def test_an_item_changed_since_the_checkpoint_is_refused(self, boundary, library):
        """Detects the concurrent-modification case -- roughly the shape of
        the 2026-08-15 incident."""
        victim = library / "Bob Seger" / "Night Moves.m4a"
        victim.write_bytes(b"CHANGED BY SOMEONE ELSE")
        with pytest.raises(PreconditionError) as exc:
            boundary.write_bytes(victim, b"NEW")
        assert exc.value.reason_code == "precondition_mismatch"

    def test_cancellation_stops_the_boundary_before_anything_changes(self, boundary, library):
        """Asserts the refusal happens BEFORE the write, not after.

        The first version only asserted the exception, and so still passed
        when the pre-write guard was removed: the post-write bookkeeping
        raised instead, having already replaced the file. An exception
        after the damage is not a refusal."""
        victim = library / "Bob Seger" / "Night Moves.m4a"
        before = _snapshot(library)
        quarantine_before = sorted(boundary.checkpoint.quarantine_root.rglob("*"))

        gate = CancellationGate(run_id="run-A", requested=True)
        gate.observe(NOW)
        boundary.gate = gate

        with pytest.raises(MutationAfterCancellationError):
            boundary.write_bytes(victim, b"NEW")

        assert _snapshot(library) == before, "the file must be untouched"
        assert sorted(boundary.checkpoint.quarantine_root.rglob("*")) == quarantine_before
        assert boundary.journal.entries() == (), "nothing may be journalled either"


# ── Quarantine-first ──────────────────────────────────────────────────────────


class TestQuarantineFirst:
    def test_a_replacement_quarantines_the_previous_bytes(self, boundary, library):
        victim = library / "Bob Seger" / "Night Moves.m4a"
        original = sha256_file(victim)
        boundary.write_bytes(victim, b"REPLACEMENT")

        assert victim.read_bytes() == b"REPLACEMENT"
        quarantined = list(boundary.checkpoint.quarantine_root.rglob("*.m4a"))
        assert len(quarantined) == 1
        assert sha256_file(quarantined[0]) == original, (
            "the displaced bytes must still exist somewhere retrievable"
        )

    def test_every_applied_operation_is_journalled(self, boundary, library):
        boundary.write_tags(library / "Bob Seger" / "Night Moves.m4a", {"artist": "Bob Seger"})
        boundary.write_artwork(library / "The Byrds" / "Eight Miles High.m4a", b"JPEGDATA")
        boundary.move(
            library / "Bob Seger" / "Hollywood Nights.m4a",
            library / "Bob Seger" / "Hollywood Nights (1978).m4a",
        )
        # Distinct OPERATIONS, not raw journal entries: a move now writes a
        # second entry recording that its source was released, because "the
        # destination exists and the source is gone" and "the destination
        # exists and the source is still there" are different recovery
        # situations and must not be inferred from one record.
        seen, kinds = set(), []
        for e in boundary.journal.applied():
            if e.operation_id in seen:
                continue
            seen.add(e.operation_id)
            kinds.append(e.operation_kind)
        assert kinds == [OP_TAG_WRITE, "artwork_write", OP_MOVE]
        # Primary entries only, for the same reason as above: a
        # release-of-source record has no precondition of its own.
        primary = {}
        for e in boundary.journal.applied():
            primary.setdefault(e.operation_id, e)
        assert all(e.precondition_digest for e in primary.values())

    def test_a_move_refuses_occupied_ground(self, boundary, library):
        with pytest.raises(CollisionError):
            boundary.move(
                library / "Bob Seger" / "Night Moves.m4a",
                library / "Bob Seger" / "Hollywood Nights.m4a",
            )


# ── Rollback restores exactly ─────────────────────────────────────────────────


class TestDeferredSourceRelease:
    """move(release_source=False) keeps a verified copy on disk across the
    window where the caller's own work can still fail -- FinalizeStage's
    archive-row UPDATE can hit a UNIQUE collision after the file is in
    place, and losing the source at that moment is unrecoverable."""

    def test_source_survives_until_released(self, boundary, library):
        src = library / "Bob Seger" / "Night Moves.m4a"
        dst = library / "The Byrds" / "Night Moves.m4a"
        digest = sha256_file(src)

        op = boundary.move(src, dst, release_source=False)
        assert dst.is_file() and sha256_file(dst) == digest
        assert src.is_file(), "source must remain until the caller releases it"

        boundary.release_source(op, src)
        assert not src.exists()
        assert sha256_file(dst) == digest

    def test_a_failed_copy_leaves_the_source_untouched(self, boundary, library, monkeypatch):
        """The reason this is not shutil.move: a cross-filesystem move is
        copy-then-delete with no verification between, so an interrupted
        one can destroy the source having written a short destination."""
        import musaeus.safety.mutation as mut

        src = library / "Bob Seger" / "Night Moves.m4a"
        before = sha256_file(src)
        monkeypatch.setattr(
            mut.shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        )

        with pytest.raises(OSError):
            boundary.move(src, library / "The Byrds" / "Night Moves.m4a")

        assert src.is_file() and sha256_file(src) == before
        assert not (library / "The Byrds" / "Night Moves.m4a").exists()
        assert not list(library.rglob("*.mutation_tmp"))

    def test_a_short_copy_is_caught_and_the_source_kept(self, boundary, library, monkeypatch):
        import musaeus.safety.mutation as mut

        src = library / "Bob Seger" / "Night Moves.m4a"
        before = sha256_file(src)
        real = mut.shutil.copy2

        def truncating(a, b, *args, **kw):
            real(a, b, *args, **kw)
            Path(b).write_bytes(Path(b).read_bytes()[:3])  # short write

        monkeypatch.setattr(mut.shutil, "copy2", truncating)

        with pytest.raises(CollisionError) as exc:
            boundary.move(src, library / "The Byrds" / "Night Moves.m4a")
        assert "size mismatch" in str(exc.value)
        assert src.is_file() and sha256_file(src) == before


class TestRollbackRestoresExactly:
    def test_rollback_after_a_mixed_batch_restores_hashes_and_locations(self, boundary, library):
        before = _snapshot(library)

        boundary.write_tags(library / "Bob Seger" / "Night Moves.m4a", {"artist": "Bob Seger"})
        boundary.write_artwork(library / "The Byrds" / "Eight Miles High.m4a", b"JPEGDATA")
        boundary.move(
            library / "Bob Seger" / "Hollywood Nights.m4a",
            library / "Bob Seger" / "Hollywood Nights (1978).m4a",
        )
        assert _snapshot(library) != before

        result = boundary.rollback(now=NOW)

        assert result.outcome == ROLLBACK_COMPLETED
        assert _snapshot(library) == before, "every hash and every location must be back"

    def test_rollback_after_a_quarantine_puts_the_file_back(self, boundary, library):
        before = _snapshot(library)
        boundary.quarantine(library / "The Byrds" / "Eight Miles High.m4a", reason="tribute")
        assert not (library / "The Byrds" / "Eight Miles High.m4a").exists()

        boundary.rollback(now=NOW)
        assert _snapshot(library) == before

    def test_rollback_restores_the_database(self, library, tmp_path):
        recovery = tmp_path / "recovery"
        recovery.mkdir()
        db = tmp_path / "musaeus.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE archive (file_path TEXT, artist TEXT)")
        conn.execute("INSERT INTO archive VALUES ('/x/a.m4a', 'Bob Seger')")
        conn.commit()
        conn.close()

        checkpoint = create_checkpoint(
            library, recovery, checkpoint_id="c1", database_path=db, now=NOW
        )
        journal = OperationJournal(checkpoint.root / JOURNAL_FILENAME)
        boundary = MutationBoundary(checkpoint, journal, run_id="run-A", source_root=library)

        boundary.record_database_write("deleted everything, as one does")
        conn = sqlite3.connect(str(db))
        conn.execute("DELETE FROM archive")
        conn.commit()
        conn.close()

        boundary.rollback(database_path=db, now=NOW)

        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute("SELECT artist FROM archive").fetchall()
        finally:
            conn.close()
        assert [r[0] for r in rows] == ["Bob Seger"]

    def test_rollback_is_idempotent(self, boundary, library):
        before = _snapshot(library)
        boundary.write_bytes(library / "Bob Seger" / "Night Moves.m4a", b"REPLACEMENT")
        boundary.rollback(now=NOW)
        second = boundary.rollback(now=NOW)
        assert second.outcome == ROLLBACK_COMPLETED
        assert _snapshot(library) == before

    def test_rollback_marks_operations_restored_without_editing_history(self, boundary, library):
        boundary.write_bytes(library / "Bob Seger" / "Night Moves.m4a", b"REPLACEMENT")
        applied_id = boundary.journal.entries()[0].operation_id
        boundary.rollback(now=NOW)

        entries = boundary.journal.entries()
        assert entries[0].operation_id == applied_id
        assert entries[0].status == "applied", "the original record survives"
        assert any(e.status == STATUS_RESTORED for e in entries)

    def test_rollback_undoes_in_reverse_order(self, boundary, library):
        """A tag write on a file that was then moved must be undone before
        the move, or the restore lands at a path that no longer holds it."""
        before = _snapshot(library)
        source = library / "Bob Seger" / "Night Moves.m4a"
        moved = library / "Bob Seger" / "Night Moves (1976).m4a"
        boundary.write_tags(source, {"year": "1976"})
        boundary.move(source, moved)
        assert moved.exists() and not source.exists()

        boundary.rollback(now=NOW)
        assert _snapshot(library) == before


# ── Rollback refuses to cause loss ────────────────────────────────────────────


class TestRollbackRefusesUnexpectedOverwrite:
    def test_a_change_made_after_the_mutation_blocks_the_restore(self, boundary, library):
        """A rollback that overwrites someone else's later change has not
        recovered anything; it has lost data twice."""
        victim = library / "Bob Seger" / "Night Moves.m4a"
        boundary.write_bytes(victim, b"REPLACEMENT")
        victim.write_bytes(b"A LATER, DIFFERENT CHANGE")

        with pytest.raises(RollbackFailedError) as exc:
            boundary.rollback(now=NOW)

        assert exc.value.reason_code == "rollback_failed"
        assert exc.value.details["failures"][0]["reason_code"] == "recovery_collision"
        assert victim.read_bytes() == b"A LATER, DIFFERENT CHANGE", (
            "the later change must be left exactly where it is"
        )

    def test_a_failed_rollback_preserves_all_recovery_material(self, boundary, library):
        victim = library / "Bob Seger" / "Night Moves.m4a"
        original = sha256_file(victim)
        boundary.write_bytes(victim, b"REPLACEMENT")
        victim.write_bytes(b"A LATER, DIFFERENT CHANGE")

        with pytest.raises(RollbackFailedError):
            boundary.rollback(now=NOW)

        quarantined = list(boundary.checkpoint.quarantine_root.rglob("*.m4a"))
        assert quarantined, "quarantine material must survive a failed rollback"
        assert sha256_file(quarantined[0]) == original
        assert boundary.checkpoint.payload_root.is_dir(), "the checkpoint must survive too"
        assert boundary.journal.entries(), "the journal must survive too"

    def test_rollback_never_deletes_a_created_file(self, boundary, library):
        """Undoing a creation cannot mean deleting: MCR-003 forbids
        permanent deletion in P0, so the file is quarantined instead."""
        created = library / "Bob Seger" / "Brand New.m4a"
        boundary.write_bytes(created, b"NEWLY CREATED")
        assert created.exists()

        boundary.rollback(now=NOW)

        assert not created.exists(), "the tree is back to its checkpointed shape"
        recovered = [
            p for p in boundary.checkpoint.quarantine_root.rglob("Brand New.m4a") if p.is_file()
        ]
        assert recovered, "the created file is retrievable, not destroyed"
        assert recovered[0].read_bytes() == b"NEWLY CREATED"
