"""The September ultrareview's safety-layer findings (PR #22), as tests.

Each was reproduced on 2026-10-07 by the triage script against the code of
that day. A and B are one fix: B (the checkpoint is rooted at STAGING, the
boundary at the vault, so the checkpoint is never consulted) hides A (a
tag-captured file can never match its own checkpoint record), and fixing
B alone would make finalize refuse every tagged file it moves.

The move-undo test is not from the review. It was found while fixing B:
finalize's DB-collision path removes a move's destination by hand, and
rollback then took the "run created this" branch and quarantined the
move's ORIGIN -- for an INBOX passthrough, the only copy.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.safety import recovery
from musaeus.safety.manifest import (
    KIND_FILE,
    KIND_TAGGED_AUDIO,
    Manifest,
    item_ref_for,
    read_tags,
)
from musaeus.safety.mutation import (
    MutationBoundary,
    PreconditionError,
    RollbackFailedError,
)
from musaeus.safety.recovery import (
    JOURNAL_FILENAME,
    OP_TAG_WRITE,
    STATUS_RESTORED,
    Checkpoint,
    CollisionError,
    OperationJournal,
    create_checkpoint,
)
from musaeus.stages.finalize import FinalizeStage

_CODECS = {".m4a": ["-c:a", "alac"], ".flac": ["-c:a", "flac"], ".mp3": ["-c:a", "libmp3lame"]}


def _need_audio_tools() -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg unavailable")
    pytest.importorskip("mutagen")


def _tagged_audio(path: Path, title: str = "Original") -> Path:
    """A real, parseable, tagged file. FLAC gets a two-valued artist,
    because a Vorbis comment can hold a key more than once."""
    import mutagen
    from mutagen.id3 import TIT2, TPE1

    _need_audio_tools()
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=f=440:d=1",
            *_CODECS[path.suffix],
            str(path),
        ],
        check=True,
    )
    audio = mutagen.File(str(path))
    if audio.tags is None:
        audio.add_tags()
    if path.suffix == ".mp3":
        audio.tags.add(TIT2(encoding=3, text=[title]))
        audio.tags.add(TPE1(encoding=3, text=["Artist"]))
    elif path.suffix == ".flac":
        audio.tags["title"] = [title]
        audio.tags["artist"] = ["First Artist", "Second Artist"]
    else:
        audio.tags["\xa9nam"] = [title]
        audio.tags["\xa9ART"] = ["Artist"]
    audio.save()
    return path


def _retitle(path: Path, title: str) -> None:
    import mutagen
    from mutagen.id3 import TIT2

    audio = mutagen.File(str(path))
    if path.suffix == ".mp3":
        audio.tags.add(TIT2(encoding=3, text=[title]))
    elif path.suffix == ".flac":
        audio.tags["title"] = [title]
    else:
        audio.tags["\xa9nam"] = [title]
    audio.save()


def _boundary(
    checkpoint_root: Path,
    recovery_root: Path,
    boundary_root: Path,
    *,
    capture_tags: bool = False,
    cid: str = "ck",
):
    recovery_root.mkdir(parents=True, exist_ok=True)
    checkpoint = create_checkpoint(
        checkpoint_root,
        recovery_root,
        checkpoint_id=cid,
        capture_tags=capture_tags,
        reserve_bytes=0,
        reserve_fraction=0,
    )
    journal = OperationJournal(checkpoint.root / JOURNAL_FILENAME)
    return MutationBoundary(checkpoint, journal, run_id="run", source_root=boundary_root), journal


# ── A: a tag-captured file must pass its own precondition ───────────────────


class TestA_TaggedIdentity:
    @pytest.mark.parametrize("suffix", [".m4a", ".flac"])
    def test_untouched_tagged_file_can_be_moved(self, tmp_path, suffix):
        lib = tmp_path / "lib"
        song = _tagged_audio(lib / f"song{suffix}")
        boundary, _ = _boundary(lib, tmp_path / "rec", lib, capture_tags=True)
        assert boundary.checkpoint.manifest.entries[0].kind == KIND_TAGGED_AUDIO

        boundary.move(song, lib / f"moved{suffix}")

        assert (lib / f"moved{suffix}").is_file() and not song.exists()

    def test_tagged_file_retagged_since_the_checkpoint_is_refused(self, tmp_path):
        lib = tmp_path / "lib"
        song = _tagged_audio(lib / "song.m4a")
        boundary, _ = _boundary(lib, tmp_path / "rec", lib, capture_tags=True)
        _retitle(song, "Changed underneath the run")

        with pytest.raises(PreconditionError):
            boundary.move(song, lib / "moved.m4a")
        assert song.is_file()


# ── B: the checkpoint is consulted under finalize's and canonicalize's roots ─


class TestB_CheckpointRoot:
    def test_finalize_layout_detects_a_concurrent_change(self, tmp_path):
        vault = tmp_path / "vault"
        staged = vault / "STAGING" / "a.txt"
        staged.parent.mkdir(parents=True)
        staged.write_text("checkpointed\n")
        boundary, _ = _boundary(vault / "STAGING", tmp_path / "rec", vault)
        staged.write_text("CONCURRENTLY MODIFIED\n")

        with pytest.raises(PreconditionError):
            boundary.write_bytes(staged, b"run output\n")
        assert staged.read_text() == "CONCURRENTLY MODIFIED\n"

    def test_canonicalize_layout_rollback_brings_the_inbox_original_back(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "STAGING").mkdir(parents=True)
        (vault / "STAGING" / "1_orig.m4a").write_bytes(b"staged output")
        original = vault / "INBOX" / "orig.flac"
        original.parent.mkdir()
        original.write_bytes(b"the only copy of the original")
        boundary, _ = _boundary(vault / "STAGING", tmp_path / "rec", vault)

        boundary.quarantine(original, reason="canonicalized to STAGING/1_orig.m4a")
        assert not original.exists()
        boundary.rollback()

        assert original.read_bytes() == b"the only copy of the original"

    def test_quarantine_rollback_works_from_the_journal_alone(self, tmp_path):
        """A resumed rollback builds a fresh boundary, which has no memory of
        the quarantines. The journal has to be enough."""
        vault = tmp_path / "vault"
        (vault / "STAGING").mkdir(parents=True)
        original = vault / "INBOX" / "orig.flac"
        original.parent.mkdir()
        original.write_bytes(b"original bytes")
        boundary, journal = _boundary(vault / "STAGING", tmp_path / "rec", vault)
        boundary.quarantine(original, reason="test")

        fresh = MutationBoundary(
            boundary.checkpoint, OperationJournal(journal.path), run_id="run", source_root=vault
        )
        fresh.rollback()
        fresh.rollback()  # idempotent

        assert original.read_bytes() == b"original bytes"

    def test_boundary_refuses_a_checkpoint_outside_its_root(self, tmp_path):
        """Rooting the boundary somewhere the checkpoint is not is the shape
        of B. Refuse it when the boundary is made, not after a rollback."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (tmp_path / "vault").mkdir()
        with pytest.raises(Exception, match="checkpoint"):
            _boundary(elsewhere, tmp_path / "rec", tmp_path / "vault")


# ── Undoing a move never quarantines its origin ─────────────────────────────


class TestMoveUndo:
    @pytest.mark.parametrize("area", ["STAGING", "INBOX"])
    def test_destination_removed_by_hand_leaves_the_origin_alone(self, tmp_path, area):
        """finalize's DB-collision path: the copy lands, the archive UPDATE
        collides, finalize unlinks the copy itself and keeps the source."""
        vault = tmp_path / "vault"
        (vault / "STAGING").mkdir(parents=True)
        source = vault / area / "song.m4a"
        source.parent.mkdir(exist_ok=True)
        source.write_bytes(b"the song")
        boundary, _ = _boundary(vault / "STAGING", tmp_path / "rec", vault)
        destination = vault / "ALAC-Archival" / "song.m4a"

        boundary.move(source, destination, release_source=False)
        destination.unlink()
        boundary.rollback()

        assert source.read_bytes() == b"the song", "rollback must not quarantine a move's origin"

    def test_both_ends_gone_is_reported_not_called_restored(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "STAGING").mkdir(parents=True)
        source = vault / "INBOX" / "song.m4a"
        source.parent.mkdir()
        source.write_bytes(b"the song")
        boundary, _ = _boundary(vault / "STAGING", tmp_path / "rec", vault)
        destination = vault / "ALAC-Archival" / "song.m4a"
        boundary.move(source, destination)
        destination.unlink()

        with pytest.raises(RollbackFailedError) as caught:
            boundary.rollback()
        assert caught.value.details["result"].remaining_operations == 1


# ── Finalize itself, on a real tagged ALAC (the A+B regression guard) ───────


def test_finalize_moves_and_rolls_back_a_real_tagged_alac(tmp_path):
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library",
        alac_archive=tmp_path / "ALAC-Archival",
        db_path=tmp_path / "musaeus.db",
    )
    cfg.ensure_dirs()
    ctx = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    ctx.set("finalize_batch_date", "2026-01-15")
    staged = _tagged_audio(cfg.staging / "Bob Seger - Night Moves.m4a")
    original = staged.read_bytes()
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(staged),
            "status": "CATALOGUED",
            "artist": "Bob Seger",
            "album": "Night Moves",
            "title": "Night Moves",
            "audio_hash": "cafe0001",
        },
    )
    ctx.conn.execute(
        "UPDATE archive SET canonicalized_at = datetime('now'), "
        "canon_action = 'CONVERTED' WHERE file_path = ?",
        (str(staged),),
    )
    ctx.conn.commit()

    result = FinalizeStage().run(ctx)

    root = cfg.runs_root / "recovery" / f"finalize_{ctx.run_id}"
    manifest = Manifest.from_json((root / "manifest.json").read_text())
    assert [e.kind for e in manifest.entries] == [KIND_TAGGED_AUDIO]
    assert result.files_errored == 0, result.errors
    assert len(list(cfg.alac_archive.rglob("*.m4a"))) == 1 and not staged.exists()

    checkpoint = Checkpoint(
        checkpoint_id=manifest.checkpoint_id,
        root=root,
        manifest=manifest,
        manifest_digest=manifest.digest,
        created_at=manifest.created_at,
        verified=True,
        recovery_target=str(root.parent),
    )
    MutationBoundary(
        checkpoint,
        OperationJournal(root / JOURNAL_FILENAME),
        run_id=ctx.run_id,
        source_root=cfg.vault_root,
    ).rollback()

    assert staged.read_bytes() == original
    assert list(cfg.alac_archive.rglob("*.m4a")) == []


# ── F1: tags come back on FLAC; MP3 is checkpointed by copy ─────────────────


class TestF1_RestoreTags:
    def test_flac_tags_round_trip_through_rollback(self, tmp_path):
        lib = tmp_path / "lib"
        song = _tagged_audio(lib / "song.flac")
        before = read_tags(song)
        assert before["artist"] == [
            {"t": "str", "v": "First Artist"},
            {"t": "str", "v": "Second Artist"},
        ]
        boundary, journal = _boundary(lib, tmp_path / "rec", lib, capture_tags=True)

        _retitle(song, "MANGLED")
        journal.append(
            operation_kind=OP_TAG_WRITE,
            item_ref=item_ref_for("song.flac"),
            detail={"relative_path": "song.flac"},
        )
        boundary.rollback()

        assert read_tags(song) == before

    def test_mp3_is_checkpointed_by_copy_and_restored(self, tmp_path):
        """ID3 frames are not plain values; putting them back from the
        manifest is not supported, so an MP3 is copied instead."""
        lib = tmp_path / "lib"
        song = _tagged_audio(lib / "song.mp3")
        original = song.read_bytes()
        boundary, journal = _boundary(lib, tmp_path / "rec", lib, capture_tags=True)
        assert boundary.checkpoint.manifest.entries[0].kind == KIND_FILE

        boundary.write_bytes(song, b"replaced")
        boundary.rollback()

        assert song.read_bytes() == original


# ── F2: rolling back a quarantine never overwrites newer content ────────────


def test_F2_rollback_of_a_quarantine_keeps_work_that_arrived_since(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    path = lib / "a.txt"
    path.write_text("checkpointed\n")
    boundary, _ = _boundary(lib, tmp_path / "rec", lib)
    boundary.quarantine(path, reason="test")
    path.write_text("NEW WORK written after the quarantine\n")

    with pytest.raises(RollbackFailedError):
        boundary.rollback()
    assert path.read_text() == "NEW WORK written after the quarantine\n"


# ── F3: a checkpointed file that vanished is not "new" ──────────────────────


def test_F3_write_bytes_on_a_vanished_checkpointed_file_is_refused(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    path = lib / "b.txt"
    path.write_text("checkpointed\n")
    boundary, _ = _boundary(lib, tmp_path / "rec", lib)
    path.unlink()  # a concurrent process removes it

    with pytest.raises(PreconditionError):
        boundary.write_bytes(path, b"fresh\n")
    assert not path.exists()


# ── F4: one failed restore does not abandon the rest ────────────────────────


def test_F4_an_unexpected_error_is_reported_and_the_rest_restored(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "one.txt").write_text("one original\n")
    (lib / "two.txt").write_text("two original\n")
    boundary, journal = _boundary(lib, tmp_path / "rec", lib)
    first = boundary.write_bytes(lib / "one.txt", b"one CHANGED\n")
    boundary.write_bytes(lib / "two.txt", b"two CHANGED\n")  # undone first

    real_copy2 = shutil.copy2

    def copy2(src, dst, *a, **kw):
        if Path(dst).name == "two.txt":
            raise PermissionError("injected")
        return real_copy2(src, dst, *a, **kw)

    monkeypatch.setattr("musaeus.safety.mutation.shutil.copy2", copy2)

    with pytest.raises(RollbackFailedError) as caught:
        boundary.rollback()

    result = caught.value.details["result"]
    assert len(result.failures) == 1 and result.failures[0]["message"] == "injected"
    assert (lib / "one.txt").read_text() == "one original\n"
    assert any(e.operation_id == first and e.status == STATUS_RESTORED for e in journal.entries())


# ── F6: the journal does not re-read itself for every operation ─────────────


class TestF6_JournalCost:
    def _count_parses(self, monkeypatch) -> list[int]:
        calls = [0]
        real = recovery.json.loads

        def counting(*a, **kw):
            calls[0] += 1
            return real(*a, **kw)

        monkeypatch.setattr(recovery.json, "loads", counting)
        return calls

    def test_boundary_operations_and_rollback_stay_linear(self, tmp_path, monkeypatch):
        lib = tmp_path / "lib"
        lib.mkdir()
        for n in range(300):
            (lib / f"{n}.txt").write_text(f"{n}\n")
        boundary, _ = _boundary(lib, tmp_path / "rec", lib)
        for n in range(290):
            boundary.write_bytes(lib / f"{n}.txt", b"changed\n")

        calls = self._count_parses(monkeypatch)
        for n in range(290, 300):
            boundary.write_bytes(lib / f"{n}.txt", b"changed\n")
        assert calls[0] < 50, f"10 operations parsed {calls[0]} journal lines"

        calls[0] = 0
        boundary.rollback()
        assert calls[0] < 5 * 600, f"rolling back 300 operations parsed {calls[0]} lines"

    def test_another_instance_appending_stays_visible(self, tmp_path):
        path = tmp_path / "journal.jsonl"
        first = OperationJournal(path)
        first.append(operation_kind=OP_TAG_WRITE, item_ref="a")
        assert len(first.entries()) == 1
        OperationJournal(path).append(operation_kind=OP_TAG_WRITE, item_ref="b")

        third = first.append(operation_kind=OP_TAG_WRITE, item_ref="c")

        assert [e.item_ref for e in first.entries()] == ["a", "b", "c"]
        assert third.sequence == 2


# ── Review of #87, finding 2: undoing a move clears the copy it made ──────


def test_undoing_a_move_whose_source_was_kept_clears_the_copy(tmp_path):
    """Finalize keeps the source until the archive row lands. Undoing such a
    move left the copy at the destination and called it restored, so a re-run
    made a "(2)" second copy beside an untracked first one."""
    vault = tmp_path / "vault"
    source = vault / "STAGING" / "song.m4a"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"the song")
    boundary, _ = _boundary(vault / "STAGING", tmp_path / "rec", vault)
    destination = vault / "ALAC-Archival" / "song.m4a"

    boundary.move(source, destination, release_source=False)
    result = boundary.rollback()

    assert source.read_bytes() == b"the song"
    assert not destination.exists(), "the copy was left behind"
    assert result.outcome == "completed"


# ── Review of #87, finding 8: a move checks content, not size ──────────────


def test_a_damaged_copy_of_the_same_size_never_releases_the_source(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    source = lib / "song.bin"
    source.write_bytes(b"the real song" * 100)
    boundary, _ = _boundary(lib, tmp_path / "rec", lib)
    real_copy2 = shutil.copy2

    def damaging_copy2(src, dst, *a, **kw):
        real_copy2(src, dst, *a, **kw)
        data = bytearray(Path(dst).read_bytes())
        data[10] ^= 0xFF  # one flipped byte, same size
        Path(dst).write_bytes(bytes(data))

    monkeypatch.setattr("musaeus.safety.mutation.shutil.copy2", damaging_copy2)

    with pytest.raises(CollisionError):
        boundary.move(source, lib / "moved" / "song.bin")

    assert source.read_bytes() == b"the real song" * 100
    assert not (lib / "moved" / "song.bin").exists()


# ── Review of #87, minor finding 1: a file written twice rolls back clean ──


def test_a_file_written_twice_rolls_back_without_a_false_failure(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    path = lib / "a.txt"
    path.write_text("original\n")
    boundary, _ = _boundary(lib, tmp_path / "rec", lib)
    boundary.write_bytes(path, b"first\n")
    boundary.write_bytes(path, b"second\n")

    result = boundary.rollback()

    assert path.read_text() == "original\n"
    assert result.outcome == "completed", "the restore worked but was reported as failed"
