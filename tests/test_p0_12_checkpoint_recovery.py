"""
P0-12 — checkpoint, manifest, journal and quarantine primitives.

The assertions that matter here are about *evidence*, not about calls:

  * a checkpoint is verified by re-hashing every copied byte, and a
    corrupted copy is caught (proven by corrupting one);
  * the journal survives losing the process that wrote it;
  * quarantine moves and never deletes -- asserted against the module's
    public API, not just its behaviour on one path;
  * a collision is refused rather than resolved by overwriting.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from musaeus.safety.manifest import (
    KIND_DATABASE,
    KIND_FILE,
    Manifest,
    build_manifest,
    item_ref_for,
    sha256_file,
)
from musaeus.safety.recovery import (
    JOURNAL_FILENAME,
    OP_MOVE,
    OP_TAG_WRITE,
    STATUS_APPLIED,
    STATUS_RESTORED,
    CheckpointCapacityError,
    CheckpointError,
    CollisionError,
    JournalError,
    OperationJournal,
    create_checkpoint,
    quarantine_item,
    restore_quarantined,
    verify_checkpoint,
)
from musaeus.state.policy import RECOVERY_CAP_BYTES

NOW = "2026-08-23T22:00:00Z"


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "library"
    (root / "Bob Seger").mkdir(parents=True)
    (root / "The Byrds").mkdir(parents=True)
    (root / "Bob Seger" / "Night Moves.m4a").write_bytes(b"ALAC PAYLOAD ONE")
    (root / "Bob Seger" / "Hollywood Nights.m4a").write_bytes(b"ALAC PAYLOAD TWO")
    (root / "The Byrds" / "Eight Miles High.m4a").write_bytes(b"ALAC PAYLOAD THREE")
    return root


@pytest.fixture
def recovery(tmp_path: Path) -> Path:
    root = tmp_path / "recovery"
    root.mkdir()
    return root


# ── Manifest ──────────────────────────────────────────────────────────────────


class TestManifest:
    def test_entries_are_ordered_by_path_not_by_filesystem_order(self, library):
        manifest = build_manifest(library, checkpoint_id="c1", created_at=NOW)
        paths = [e.relative_path for e in manifest.entries]
        assert paths == sorted(paths)
        assert len(paths) == 3

    def test_digest_is_stable_for_identical_content(self, library, tmp_path):
        a = build_manifest(library, checkpoint_id="c1", created_at=NOW)
        b = build_manifest(library, checkpoint_id="c1", created_at=NOW)
        assert a.digest == b.digest

    def test_digest_changes_when_any_covered_byte_changes(self, library):
        before = build_manifest(library, checkpoint_id="c1", created_at=NOW).digest
        (library / "Bob Seger" / "Night Moves.m4a").write_bytes(b"ALAC PAYLOAD ONE!")
        after = build_manifest(library, checkpoint_id="c1", created_at=NOW).digest
        assert before != after

    def test_item_ref_does_not_leak_the_path(self, library):
        manifest = build_manifest(library, checkpoint_id="c1", created_at=NOW)
        entry = manifest.entries[0]
        assert "Bob Seger" not in entry.item_ref
        assert entry.item_ref == item_ref_for(entry.relative_path)
        assert len(entry.item_ref) == 24

    def test_database_is_covered_as_its_own_entry_kind(self, library, tmp_path):
        db = tmp_path / "musaeus.db"
        db.write_bytes(b"SQLite format 3\x00 not really")
        manifest = build_manifest(library, checkpoint_id="c1", created_at=NOW, database_path=db)
        kinds = {e.kind for e in manifest.entries}
        assert kinds == {KIND_FILE, KIND_DATABASE}
        assert manifest.coverage()["databases"] == 1
        assert manifest.coverage()["files"] == 3

    def test_coverage_reports_counts_and_bytes(self, library):
        coverage = build_manifest(library, checkpoint_id="c1", created_at=NOW).coverage()
        assert coverage["items"] == 3
        assert coverage["bytes"] == sum(p.stat().st_size for p in library.rglob("*") if p.is_file())

    def test_round_trips_through_json(self, library):
        manifest = build_manifest(library, checkpoint_id="c1", created_at=NOW)
        assert Manifest.from_json(manifest.to_json()).digest == manifest.digest


# ── Checkpoint ────────────────────────────────────────────────────────────────


class TestCheckpoint:
    def test_checkpoint_copies_and_verifies_every_byte(self, library, recovery):
        checkpoint = create_checkpoint(library, recovery, checkpoint_id="c1", now=NOW)
        assert checkpoint.verified is True
        for entry in checkpoint.manifest.entries:
            copied = checkpoint.payload_root / entry.relative_path
            assert copied.is_file()
            assert sha256_file(copied) == entry.sha256

    def test_a_corrupted_copy_fails_verification(self, library, recovery):
        """The decisive test. A truncated or partially written copy has a
        plausible size and the wrong content; only re-hashing catches it."""
        checkpoint = create_checkpoint(library, recovery, checkpoint_id="c1", now=NOW)
        victim = checkpoint.payload_root / "Bob Seger" / "Night Moves.m4a"
        victim.write_bytes(b"CORRUPTED")

        with pytest.raises(CheckpointError) as exc:
            verify_checkpoint(checkpoint)
        assert "does not match its manifest digest" in str(exc.value)

    def test_a_missing_copy_fails_verification(self, library, recovery):
        checkpoint = create_checkpoint(library, recovery, checkpoint_id="c1", now=NOW)
        (checkpoint.payload_root / "The Byrds" / "Eight Miles High.m4a").unlink()
        with pytest.raises(CheckpointError) as exc:
            verify_checkpoint(checkpoint)
        assert "missing" in str(exc.value)

    def test_a_tampered_manifest_fails_verification(self, library, recovery):
        checkpoint = create_checkpoint(library, recovery, checkpoint_id="c1", now=NOW)
        data = json.loads(checkpoint.manifest_path.read_text())
        data["entries"][0]["sha256"] = "0" * 64
        checkpoint.manifest_path.write_text(json.dumps(data))
        with pytest.raises(CheckpointError) as exc:
            verify_checkpoint(checkpoint)
        assert "digest does not match" in str(exc.value)

    def test_capacity_over_the_fixed_cap_blocks_before_copying(
        self, library, recovery, monkeypatch
    ):
        import musaeus.safety.recovery as recovery_mod

        monkeypatch.setattr(
            recovery_mod, "build_manifest", _oversized_manifest(RECOVERY_CAP_BYTES + 1)
        )
        with pytest.raises(CheckpointCapacityError) as exc:
            create_checkpoint(library, recovery, checkpoint_id="c1", now=NOW)
        assert exc.value.details["cap"] == RECOVERY_CAP_BYTES
        assert list(recovery.iterdir()) == [], "nothing may be written before the capacity check"

    def test_capacity_over_safely_usable_space_blocks(self, library, recovery):
        with pytest.raises(CheckpointCapacityError) as exc:
            create_checkpoint(library, recovery, checkpoint_id="c1", now=NOW, reserve_fraction=1.0)
        assert "safely usable" in str(exc.value)
        assert list(recovery.iterdir()) == []

    def test_an_existing_checkpoint_directory_is_a_collision_not_an_overwrite(
        self, library, recovery
    ):
        create_checkpoint(library, recovery, checkpoint_id="c1", now=NOW)
        with pytest.raises(CollisionError) as exc:
            create_checkpoint(library, recovery, checkpoint_id="c1", now=NOW)
        assert "refusing to write over it" in str(exc.value)

    def test_the_future_recovery_root_is_refused(self, library, path_guard):
        from musaeus.state.migrator import RecoveryTargetError
        from musaeus.state.policy import FUTURE_RECOVERY_ROOT

        before = len(path_guard.attempts)
        with pytest.raises(RecoveryTargetError):
            create_checkpoint(library, Path(FUTURE_RECOVERY_ROOT), checkpoint_id="c1", now=NOW)
        assert len(path_guard.attempts) == before

    def test_checkpoint_payload_is_a_valid_event_payload(self, library, recovery):
        from musaeus.state.events import CHECKPOINT_VERIFIED, new_event

        checkpoint = create_checkpoint(library, recovery, checkpoint_id="c1", now=NOW)
        event = new_event("run-A", 1, CHECKPOINT_VERIFIED, checkpoint.as_event_payload())
        assert event.payload["checkpoint_id"] == "c1"
        assert event.payload["coverage"]["items"] == 3


def _oversized_manifest(total: int):
    from musaeus.safety.manifest import Manifest as _M
    from musaeus.safety.manifest import ManifestEntry as _E

    def fake(source_root, *, checkpoint_id, created_at, database_path=None, capture_tags=False):
        return _M(
            checkpoint_id=checkpoint_id,
            created_at=created_at,
            source_root=str(source_root),
            entries=(
                _E(
                    item_ref="x" * 24,
                    relative_path="huge.m4a",
                    kind=KIND_FILE,
                    sha256="0" * 64,
                    size_bytes=total,
                    mtime_ns=0,
                ),
            ),
        )

    return fake


# ── Journal ───────────────────────────────────────────────────────────────────


class TestJournal:
    def test_entries_are_ordered_and_append_only(self, tmp_path):
        journal = OperationJournal(tmp_path / JOURNAL_FILENAME)
        first = journal.append(operation_kind=OP_TAG_WRITE, item_ref="a", now=NOW)
        second = journal.append(operation_kind=OP_MOVE, item_ref="b", now=NOW)
        entries = journal.entries()
        assert [e.sequence for e in entries] == [0, 1]
        assert [e.operation_id for e in entries] == [first.operation_id, second.operation_id]

    def test_marking_a_status_appends_rather_than_edits(self, tmp_path):
        """A journal you can rewrite is a journal that can be made to agree
        with any story."""
        journal = OperationJournal(tmp_path / JOURNAL_FILENAME)
        applied = journal.append(operation_kind=OP_MOVE, item_ref="a", now=NOW)
        journal.mark(applied.operation_id, STATUS_RESTORED, now=NOW)

        entries = journal.entries()
        assert len(entries) == 2
        assert entries[0].status == STATUS_APPLIED, "the original record is untouched"
        assert entries[1].status == STATUS_RESTORED
        assert entries[1].detail["supersedes_sequence"] == 0

    def test_unknown_operation_kind_is_refused(self, tmp_path):
        journal = OperationJournal(tmp_path / JOURNAL_FILENAME)
        with pytest.raises(JournalError) as exc:
            journal.append(operation_kind="just_a_little_delete", item_ref="a")
        assert "vocabulary is closed" in str(exc.value)

    def test_each_append_is_on_disk_before_it_returns(self, tmp_path):
        """No in-memory batching: an entry is readable by a separate handle
        the moment append() returns.

        This is the property a rollback depends on. A journal that
        accumulated entries and wrote them at close would look correct in
        every test and lose precisely the entries describing the mutations
        that were in flight when things went wrong."""
        path = tmp_path / JOURNAL_FILENAME
        journal = OperationJournal(path)
        journal.append(operation_kind=OP_MOVE, item_ref="a", now=NOW)
        assert len(path.read_text().splitlines()) == 1
        journal.append(operation_kind=OP_MOVE, item_ref="b", now=NOW)
        assert len(path.read_text().splitlines()) == 2

    def test_journal_survives_the_process_that_wrote_it(self, tmp_path):
        """Entries survive an abrupt `os._exit` with no interpreter
        shutdown and no atexit handlers.

        Scope note, because the distinction matters and a test that cannot
        fail should not claim otherwise: this proves durability against
        *process* death, which the per-append file close already provides.
        It does NOT exercise `os.fsync`, whose job is durability across
        *machine* crash and power loss -- removing the fsync leaves this
        test passing, confirmed by mutation. Simulating that needs a real
        power cut or a VM, which this suite cannot do. The fsync stays
        because the failure it guards against is real and unsimulatable
        here, not because a green test below proves it works."""
        path = tmp_path / JOURNAL_FILENAME
        script = textwrap.dedent(f"""
            import sys, os
            sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
            from pathlib import Path
            from musaeus.safety.recovery import OperationJournal, OP_MOVE
            journal = OperationJournal(Path({str(path)!r}))
            for i in range(5):
                journal.append(operation_kind=OP_MOVE, item_ref=f"item-{{i}}")
            os._exit(9)   # hard exit: no interpreter shutdown, no buffer flush
        """)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True)
        assert result.returncode == 9, result.stderr.decode()[-500:]

        entries = OperationJournal(path).entries()
        assert len(entries) == 5, "fsynced entries must survive os._exit"
        assert [e.item_ref for e in entries] == [f"item-{i}" for i in range(5)]

    def test_applied_filters_to_applied_operations(self, tmp_path):
        journal = OperationJournal(tmp_path / JOURNAL_FILENAME)
        a = journal.append(operation_kind=OP_MOVE, item_ref="a", now=NOW)
        journal.append(operation_kind=OP_MOVE, item_ref="b", now=NOW)
        journal.mark(a.operation_id, STATUS_RESTORED, now=NOW)
        assert {e.item_ref for e in journal.applied()} == {"a", "b"}


# ── Quarantine ────────────────────────────────────────────────────────────────


class TestQuarantine:
    def test_quarantine_moves_and_the_bytes_survive(self, library, recovery):
        checkpoint = create_checkpoint(library, recovery, checkpoint_id="c1", now=NOW)
        victim = library / "Bob Seger" / "Night Moves.m4a"
        original = sha256_file(victim)

        record = quarantine_item(
            victim, checkpoint, reason="tribute publisher", run_id="run-A", now=NOW
        )

        assert not victim.exists(), "the source location is vacated"
        quarantined = Path(record.quarantine_path)
        assert quarantined.is_file()
        assert sha256_file(quarantined) == original, "the bytes are moved, not destroyed"
        assert record.reason == "tribute publisher"
        assert record.run_id == "run-A"
        assert record.quarantined_at == NOW

    def test_restore_puts_it_back(self, library, recovery):
        checkpoint = create_checkpoint(library, recovery, checkpoint_id="c1", now=NOW)
        victim = library / "Bob Seger" / "Night Moves.m4a"
        original = sha256_file(victim)
        record = quarantine_item(victim, checkpoint, reason="r", run_id="run-A", now=NOW)

        restore_quarantined(record)

        assert victim.is_file()
        assert sha256_file(victim) == original

    def test_restore_is_idempotent(self, library, recovery):
        checkpoint = create_checkpoint(library, recovery, checkpoint_id="c1", now=NOW)
        victim = library / "Bob Seger" / "Night Moves.m4a"
        record = quarantine_item(victim, checkpoint, reason="r", run_id="run-A", now=NOW)
        restore_quarantined(record)
        restore_quarantined(record)
        assert victim.is_file()

    def test_restore_refuses_to_overwrite_different_content(self, library, recovery):
        """Putting the old file back over a new one would turn a rollback
        into a second act of data loss."""
        checkpoint = create_checkpoint(library, recovery, checkpoint_id="c1", now=NOW)
        victim = library / "Bob Seger" / "Night Moves.m4a"
        record = quarantine_item(victim, checkpoint, reason="r", run_id="run-A", now=NOW)
        victim.write_bytes(b"SOMETHING NEW AND DIFFERENT")

        with pytest.raises(CollisionError) as exc:
            restore_quarantined(record)
        assert "refusing to overwrite" in str(exc.value)
        assert victim.read_bytes() == b"SOMETHING NEW AND DIFFERENT"

    def test_no_permanent_delete_capability_exists(self):
        """MCR-003: 'no item is permanently deleted in P0'. The reliable
        way to honour that is not to write the function -- so this asserts
        against the module's public API, not against one code path."""
        import musaeus.safety.recovery as recovery_mod

        public = [n for n in dir(recovery_mod) if not n.startswith("_")]
        forbidden = ("delete", "purge", "destroy", "erase", "rmtree", "unlink")
        offenders = [n for n in public if any(word in n.lower() for word in forbidden)]
        assert offenders == [], f"a deletion capability appeared in the public API: {offenders}"

        source = Path(recovery_mod.__file__).read_text()
        assert "shutil.rmtree" not in source
        assert "os.remove" not in source


# ── Tag capture: checkpointing a library too large to copy ───────────────────


class TestTagCapture:
    """ALAC-Library is 468 GB against a fixed 100 GB recovery cap, so a
    byte-for-byte checkpoint of it is arithmetically impossible. It is also
    unnecessary for the stages that modify it: ForgeStage states "Never
    re-encodes audio. Tags only." and TaggerStage only saves changed
    fields. What must be restorable is the tag VALUES -- ~15 MB for the
    whole library against 483 GB of audio.

    These use REAL tracks. Fake bytes cannot exercise tag capture at all
    (mutagen reads nothing, so the entry correctly falls back to a byte
    copy), and it was real files that exposed the type bug: MP4 stores
    tmpo as an int, and the first version stringified every value.
    """

    @pytest.fixture
    def real_audio(self, tmp_path):
        """Two genuinely-parseable m4a files, synthesised here.

        NOT copies of real library tracks: the P0-01 PathGuard blocks any
        access under the real vault, and rightly so -- it caught this test
        reaching for /mnt/FORGE2TB/.../ALAC-Library on its first version.
        ffmpeg gives us a real MP4 container that mutagen can tag and read
        back, which is what tag capture actually needs exercised.
        """
        import shutil as _sh
        import subprocess

        if _sh.which("ffmpeg") is None:
            pytest.skip("ffmpeg unavailable")
        try:
            import mutagen
        except ImportError:
            pytest.skip("mutagen unavailable")

        root = tmp_path / "lib" / "Artist"
        root.mkdir(parents=True)
        for i in range(2):
            target = root / f"track{i}.m4a"
            proc = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=r=44100:cl=stereo",
                    "-t",
                    "1",
                    "-c:a",
                    "alac",
                    str(target),
                ],
                capture_output=True,
            )
            if proc.returncode != 0 or not target.is_file():
                pytest.skip(f"ffmpeg could not produce alac: {proc.stderr[-200:]!r}")
            audio = mutagen.File(str(target))
            if audio.tags is None:
                audio.add_tags()
            audio.tags["\xa9nam"] = [f"Track {i}"]
            audio.tags["\xa9ART"] = ["Bob Seger"]
            audio.tags["tmpo"] = [110]  # int -- the type that broke v1
            audio.tags["trkn"] = [(i + 1, 2)]  # int pair
            audio.save()
        return tmp_path / "lib"

    def test_tagged_audio_contributes_no_payload_bytes(self, real_audio):
        from musaeus.safety.manifest import KIND_TAGGED_AUDIO, build_manifest

        (real_audio / "notes.csv").write_text("a,b\n")
        m = build_manifest(real_audio, checkpoint_id="c", created_at=NOW, capture_tags=True)
        kinds = {e.relative_path: e.kind for e in m.entries}
        assert kinds["Artist/track0.m4a"] == KIND_TAGGED_AUDIO
        assert kinds["notes.csv"] != KIND_TAGGED_AUDIO
        assert m.payload_bytes == len("a,b\n")
        assert m.total_bytes > len("a,b\n"), "the audio contributes real bytes"

    def test_capacity_is_judged_on_payload_not_library_size(self, real_audio):
        """The cap check must see what is copied, not what exists. Judging
        by total_bytes makes an impossible checkpoint out of a tiny one."""
        from musaeus.safety.manifest import build_manifest

        m = build_manifest(real_audio, checkpoint_id="c", created_at=NOW, capture_tags=True)
        assert m.payload_bytes == 0
        assert m.total_bytes > m.payload_bytes

    def test_tagged_entries_are_not_content_hashed(self, real_audio):
        """A content hash means reading every file in full -- 483 GB for the
        real library -- and buys nothing a rollback could use, since the
        bytes are not copied. Prefixed so it cannot be mistaken for one."""
        from musaeus.safety.manifest import build_manifest

        m = build_manifest(real_audio, checkpoint_id="c", created_at=NOW, capture_tags=True)
        for e in m.entries:
            if e.relative_path.endswith(".m4a"):
                assert e.sha256.startswith("tagged:")

    def test_unreadable_audio_falls_back_to_a_byte_copy(self, tmp_path):
        """An audio file whose tags cannot be read is not tag-restorable, so
        its bytes are kept instead. Failing the whole checkpoint over one
        odd file would make a 10,000-track library un-checkpointable."""
        from musaeus.safety.manifest import KIND_FILE, build_manifest

        root = tmp_path / "lib"
        root.mkdir()
        (root / "broken.m4a").write_bytes(b"NOT REALLY AUDIO")
        m = build_manifest(root, checkpoint_id="c", created_at=NOW, capture_tags=True)
        entry = next(e for e in m.entries if e.relative_path == "broken.m4a")
        assert entry.kind == KIND_FILE
        assert entry.tags is None
        assert m.payload_bytes == len(b"NOT REALLY AUDIO")

    def test_tag_values_round_trip_with_their_types(self):
        """The failure this prevents: stringifying every value round-trips
        text fine and throws MP4MetadataValueError on the first real track,
        because MP4 stores tmpo as an int and trkn as an int pair."""
        from musaeus.safety.manifest import decode_tag_values

        encoded = [
            {"t": "str", "v": "Night Moves"},
            {"t": "int", "v": 110},
            {"t": "seq", "v": [3, 12]},
            {"t": "bool", "v": True},
        ]
        assert decode_tag_values(encoded) == ["Night Moves", 110, (3, 12), True]
        assert isinstance(decode_tag_values(encoded)[1], int)
        assert isinstance(decode_tag_values(encoded)[2], tuple)

    def test_rollback_restores_tags_without_the_bytes(self, real_audio, tmp_path):
        """End to end: capture, mangle the tags the way TaggerStage writes
        them, roll back, and get the originals back -- with no copy of the
        audio anywhere in the checkpoint."""
        import mutagen

        from musaeus.safety.manifest import item_ref_for, read_tags
        from musaeus.safety.mutation import MutationBoundary

        recovery = tmp_path / "rec"
        recovery.mkdir()
        before = {p.name: read_tags(p) for p in sorted(real_audio.rglob("*.m4a"))}

        cp = create_checkpoint(real_audio, recovery, checkpoint_id="c1", now=NOW, capture_tags=True)
        copied = sum(f.stat().st_size for f in cp.payload_root.rglob("*") if f.is_file())
        assert copied == 0, "no audio bytes may be copied"

        journal = OperationJournal(cp.root / JOURNAL_FILENAME)
        boundary = MutationBoundary(cp, journal, run_id="run-T", source_root=real_audio)
        for p in sorted(real_audio.rglob("*.m4a")):
            rel = str(p.relative_to(real_audio))
            audio = mutagen.File(str(p))
            audio.tags["\xa9nam"] = ["MANGLED"]
            audio.tags["\xa9ART"] = ["WRONG"]
            audio.save()
            journal.append(
                operation_kind=OP_TAG_WRITE,
                item_ref=item_ref_for(rel),
                detail={"relative_path": rel},
            )

        result = boundary.rollback(now=NOW)
        after = {p.name: read_tags(p) for p in sorted(real_audio.rglob("*.m4a"))}

        assert result.outcome == "completed"
        assert after == before, "every tag must come back exactly"
        assert all(p.stat().st_size > 0 for p in real_audio.rglob("*.m4a"))

    def test_verification_refuses_a_tagged_entry_with_no_tags(self, real_audio, tmp_path):
        """Its bytes were never copied, so absent tags means nothing was
        captured at all. Verifying such a checkpoint would report a backup
        that cannot restore anything -- the exact shape this project keeps
        finding, one level up."""
        from dataclasses import replace

        recovery = tmp_path / "rec2"
        recovery.mkdir()
        cp = create_checkpoint(real_audio, recovery, checkpoint_id="c9", now=NOW, capture_tags=True)
        gutted = replace(
            cp.manifest,
            entries=tuple(replace(e, tags=None) for e in cp.manifest.entries),
        )
        cp.manifest_path.write_text(gutted.to_json())
        broken = replace(cp, manifest=gutted, manifest_digest=gutted.digest)

        with pytest.raises(CheckpointError) as exc:
            verify_checkpoint(broken)
        assert "not restorable" in str(exc.value)
