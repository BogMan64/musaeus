"""A path is not an identity.

2026-09-08. A `bitrot verify` against the live vault returned this:

    files to verify: 15816
    ok: 0
    corrupt (hash mismatch): 0
    new (no baseline yet): 15816
    missing from disk (was baselined, gone now): 1385

Every baselined path was gone and every file present was unrecognised. The
check compared nothing, and reported that only as a large number beside a
green tick. The cause is that `archive_tier_hashes` keys on path, while
organize, canonicalize, finalize and the LUFS bake all move files as a
matter of course -- so a move silently orphans a baseline row.

That is the same shape as the `library files with no row: 0` incident: a
green result that means "I looked at nothing".

The fix records `audio_hash` -- the PCM identity, which ffmpeg computes from
the decoded stream and which therefore survives BOTH a move and a re-tag --
alongside the byte hash. It gives verify three answers where it had one:

    same path, same bytes                          -> ok
    different path, same PCM                       -> MOVED, not new
    same path, different bytes, same PCM           -> re-tagged, benign
    same path, different bytes, different PCM      -> ROT
    same path, different bytes, no PCM baselined   -> unclassifiable, reported

These tests use real ALAC, because every distinction above is a fact about
decoded audio and none of them can be exercised with placeholder bytes --
which is exactly why the original suite never caught this.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db
from musaeus.stages.bitrot import BitRotStage

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not available",
)


@pytest.fixture
def cfg(tmp_path: Path) -> MusicConfig:
    return MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )


@pytest.fixture
def ctx(cfg: MusicConfig) -> RunContext:
    cfg.ensure_dirs()
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _tone(path: Path, freq: int = 440, seconds: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={seconds}",
            "-c:a",
            "alac",
            str(path),
            "-y",
        ],
        check=True,
        capture_output=True,
    )
    return path


def _retag(path: Path, comment: str) -> None:
    """Rewrite the container with a new tag. Same PCM, different bytes."""
    tmp = path.with_suffix(".retag.m4a")
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-c",
            "copy",
            "-metadata",
            f"comment={comment}",
            str(tmp),
            "-y",
        ],
        check=True,
        capture_output=True,
    )
    tmp.replace(path)


def _baseline(ctx: RunContext) -> None:
    ctx.set("bitrot_rebaseline", True)
    result = BitRotStage().run(ctx)
    ctx.set("bitrot_rebaseline", False)
    assert result.success, result.errors


def _note(result, needle: str) -> str:
    for n in result.notes:
        if needle in n:
            return n
    raise AssertionError(f"no note containing {needle!r} in {result.notes}")


def test_the_baseline_records_a_pcm_identity(ctx) -> None:
    _tone(ctx.config.alac_archive / "A" / "a.m4a")
    _baseline(ctx)
    row = ctx.conn.execute("SELECT audio_hash FROM archive_tier_hashes").fetchone()
    assert row["audio_hash"], "without this, a move is indistinguishable from a new file"


def test_a_moved_file_is_recognised_not_reported_as_new(ctx) -> None:
    """The whole incident, in one test."""
    src = _tone(ctx.config.alac_archive / "Before" / "a.m4a")
    _baseline(ctx)

    moved_to = ctx.config.alac_archive / "After" / "Different Name.m4a"
    moved_to.parent.mkdir(parents=True, exist_ok=True)
    src.rename(moved_to)

    result = BitRotStage().run(ctx)

    assert "moved since baseline" in _note(result, "moved since baseline")
    assert _note(result, "new (no baseline yet").endswith("0")
    assert result.success is True, "a move is not rot and must not fail the run"


def test_a_moved_file_does_not_count_as_missing(ctx) -> None:
    """Its baseline row was claimed, so the origin path is not 'gone'."""
    src = _tone(ctx.config.alac_archive / "Before" / "a.m4a")
    _baseline(ctx)
    dest = ctx.config.alac_archive / "After" / "a.m4a"
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dest)

    result = BitRotStage().run(ctx)
    assert _note(result, "missing from disk").endswith("0")


def test_a_retagged_file_is_benign_not_rot(ctx) -> None:
    path = _tone(ctx.config.alac_archive / "A" / "a.m4a")
    _baseline(ctx)
    _retag(path, "written by the tagger, as it does on every run")

    result = BitRotStage().run(ctx)

    assert _note(result, "re-tagged, audio identical").split(":")[1].strip().startswith("1")
    assert _note(result, "corrupt (audio changed since baseline)").endswith("0")
    assert result.success is True
    assert not ctx.conn.execute(
        "SELECT 1 FROM events WHERE event_type='BITROT_DETECTED'"
    ).fetchone()


def test_changed_audio_is_rot(ctx) -> None:
    """The case the whole stage exists for must still fire."""
    path = _tone(ctx.config.alac_archive / "A" / "a.m4a", freq=440)
    _baseline(ctx)
    _tone(path, freq=880)  # same shape, different audio

    result = BitRotStage().run(ctx)

    assert result.success is False
    assert _note(result, "corrupt (audio changed since baseline)").endswith("1")
    ev = ctx.conn.execute("SELECT note FROM events WHERE event_type='BITROT_DETECTED'").fetchone()
    assert "not a re-tag" in ev["note"]


def test_a_run_that_verified_almost_nothing_is_not_a_pass(ctx) -> None:
    """15,816 unbaselined files used to print beside a green tick."""
    for i in range(4):
        _tone(ctx.config.alac_archive / "A" / f"{i}.m4a", freq=300 + 50 * i)

    result = BitRotStage().run(ctx)

    assert result.success is False
    assert "verified almost nothing" in _note(result, "verified almost nothing")


def test_a_few_new_files_among_many_baselined_still_passes(ctx) -> None:
    """The guard must not fire on ordinary growth."""
    for i in range(4):
        _tone(ctx.config.alac_archive / "A" / f"{i}.m4a", freq=300 + 50 * i)
    _baseline(ctx)
    _tone(ctx.config.alac_archive / "A" / "newcomer.m4a", freq=1000)

    result = BitRotStage().run(ctx)

    assert result.success is True
    assert _note(result, "new (no baseline yet").endswith("1")


def test_identical_audio_at_an_unbaselined_path_reads_as_moved(ctx) -> None:
    """A known and accepted ambiguity, pinned here so it stays deliberate.

    PCM identity cannot tell "this file moved" from "an exact duplicate of
    it appeared elsewhere" -- by construction, they are the same audio. The
    first frequency-blind draft of the test above tripped over exactly this.

    It is the right trade. The alternative is reporting a moved file as new,
    which is the failure this whole change exists to fix, and MUSAEUS
    deduplicates on audio_hash anyway -- two archive-tier files with one PCM
    identity is itself a condition the dupe stages are meant to resolve.
    """
    original = _tone(ctx.config.alac_archive / "A" / "a.m4a", freq=440)
    _baseline(ctx)
    duplicate = ctx.config.alac_archive / "B" / "same audio, other name.m4a"
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    duplicate.write_bytes(original.read_bytes())

    result = BitRotStage().run(ctx)

    assert _note(result, "new (no baseline yet").endswith("0")
    assert "moved since baseline" in _note(result, "moved since baseline")
    assert result.success is True


def test_backfill_gives_an_old_baseline_its_pcm_identity(ctx) -> None:
    """The migration step, without which the live vault keeps the old bug.

    A baseline taken before 2026-09-08 has a byte hash and no PCM identity.
    Re-running --rebaseline would fix that and also recompute every SHA-256;
    on the real archive that is 592 GB of work already done.
    """
    path = _tone(ctx.config.alac_archive / "A" / "a.m4a")
    _baseline(ctx)
    before = ctx.conn.execute(
        "SELECT sha256 FROM archive_tier_hashes WHERE path = ?", (str(path),)
    ).fetchone()["sha256"]
    ctx.conn.execute("UPDATE archive_tier_hashes SET audio_hash = NULL")  # the old shape
    ctx.conn.commit()

    ctx.set("bitrot_backfill_pcm", True)
    result = BitRotStage().run(ctx)
    ctx.set("bitrot_backfill_pcm", False)

    assert result.success is True
    row = ctx.conn.execute(
        "SELECT sha256, audio_hash FROM archive_tier_hashes WHERE path = ?", (str(path),)
    ).fetchone()
    assert row["audio_hash"], "the backfill did not record a PCM identity"
    assert row["sha256"] == before, "the byte baseline must never be rewritten here"


def test_backfill_lets_verify_recognise_a_move_it_would_have_missed(ctx) -> None:
    """End to end: old baseline -> backfill -> the move is seen."""
    src = _tone(ctx.config.alac_archive / "Before" / "a.m4a")
    _baseline(ctx)
    ctx.conn.execute("UPDATE archive_tier_hashes SET audio_hash = NULL")
    ctx.conn.commit()

    dest = ctx.config.alac_archive / "After" / "a.m4a"
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dest)

    before = BitRotStage().run(ctx)
    assert _note(before, "new (no baseline yet").endswith("1"), (
        "without a PCM identity the move is invisible — this is the old bug"
    )

    # The move already happened, so the backfill can no longer read the
    # origin path. Backfill is a migration to run BEFORE things move.
    src.parent.mkdir(parents=True, exist_ok=True)
    dest.rename(src)
    ctx.set("bitrot_backfill_pcm", True)
    BitRotStage().run(ctx)
    ctx.set("bitrot_backfill_pcm", False)
    src.rename(dest)

    after = BitRotStage().run(ctx)
    assert _note(after, "new (no baseline yet").endswith("0")
    assert "moved since baseline" in _note(after, "moved since baseline")
