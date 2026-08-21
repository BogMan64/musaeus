"""
Tests for CrossDupeStage — flag incoming files that already exist in
ALAC-Library from a PRIOR batch, via the persistent cross-batch hash
index (which is the only thing that survives a musaeus.db wipe between
batches).

Uses real audio_hash() calls (via ffmpeg) rather than mocking, since the
whole point of this stage is matching real audio-content hashes -- a
mock would hide exactly the kind of mismatch this stage exists to catch.
Skipped if ffmpeg is unavailable.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, open_hash_index, record_finalized_hash, upsert_archive
from musaeus.hasher import audio_hash
from musaeus.stages.cross_dupe import CrossDupeStage

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")


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
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=False)


@pytest.fixture
def ctx_dry(cfg: MusicConfig) -> RunContext:
    cfg.ensure_dirs()
    conn = open_db(cfg.db_path)
    return RunContext.new(cfg, conn, dry_run=True)


def _gen_audio(path: Path, freq: int = 440, duration: int = 1) -> None:
    import subprocess

    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={duration}",
            "-c:a",
            "alac",
            str(path),
        ],
        capture_output=True,
        check=True,
    )


def _seed_prior_batch_hash(
    ctx: RunContext,
    real_audio_path: Path,
    library_path: str,
    *,
    place_on_disk: bool = True,
) -> str:
    """Simulate a file already finalized in a PRIOR batch: compute its
    real audio hash and record it directly in the persistent index,
    without going through Finalize.

    place_on_disk puts a real file at library_path, because that is what
    "already finalized into ALAC-Library" actually means. Until 2026-08-21
    this helper only ever wrote the ledger row, so every test here modelled
    a library that claimed to hold a file it did not have -- and the three
    tests using it passed only because the stage never checked. That gap is
    the dupe cascade in miniature; pass place_on_disk=False to test it
    deliberately.
    """
    h = audio_hash(real_audio_path)
    if place_on_disk:
        dest = Path(library_path)
        if dest != real_audio_path:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(real_audio_path, dest)
    hash_conn = open_hash_index(ctx.config.hash_index_path)
    record_finalized_hash(hash_conn, h, library_path)
    hash_conn.commit()
    hash_conn.close()
    return h


def _register_hashed(ctx: RunContext, path: Path, audio_hash_val: str) -> None:
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(path),
            "status": "HASHED",
            "audio_hash": audio_hash_val,
        },
    )
    ctx.conn.commit()


class TestCrossDupeDetection:
    def test_matching_hash_flagged(self, ctx):
        # "Prior batch" file, same audio content as what's arriving now.
        prior_source = ctx.vault_root / "prior_source.m4a"
        _gen_audio(prior_source, freq=440)
        h = _seed_prior_batch_hash(
            ctx, prior_source, str(ctx.alac_library / "Artist" / "Album" / "Existing.m4a")
        )

        # This batch's file has the identical audio content.
        incoming = ctx.inbox / "new_arrival.m4a"
        _gen_audio(incoming, freq=440)
        _register_hashed(ctx, incoming, h)

        result = CrossDupeStage().execute(ctx)

        assert result.files_changed == 1
        row = ctx.conn.execute(
            "SELECT duplicate_type FROM duplicates WHERE file_path = ?",
            (str(incoming),),
        ).fetchone()
        assert row is not None
        assert row["duplicate_type"] == "CROSS_BATCH"

    def test_non_matching_hash_not_flagged(self, ctx):
        prior_source = ctx.vault_root / "prior_source.m4a"
        _gen_audio(prior_source, freq=440)
        _seed_prior_batch_hash(
            ctx, prior_source, str(ctx.alac_library / "Artist" / "Album" / "Existing.m4a")
        )

        # Genuinely different audio content.
        incoming = ctx.inbox / "different_song.m4a"
        _gen_audio(incoming, freq=880)
        different_hash = audio_hash(incoming)
        _register_hashed(ctx, incoming, different_hash)

        result = CrossDupeStage().execute(ctx)

        assert result.files_changed == 0
        row = ctx.conn.execute(
            "SELECT * FROM duplicates WHERE file_path = ?", (str(incoming),)
        ).fetchone()
        assert row is None

    def test_no_index_yet_is_a_clean_noop(self, ctx):
        """First batch ever, or nothing finalized so far -- no
        hash_index.db exists at all. Must not error."""
        incoming = ctx.inbox / "track.m4a"
        _gen_audio(incoming)
        h = audio_hash(incoming)
        _register_hashed(ctx, incoming, h)

        assert not ctx.config.hash_index_path.exists()

        result = CrossDupeStage().execute(ctx)

        assert result.success is True
        assert result.files_changed == 0
        assert any("no cross-batch hash index" in n for n in result.notes)


class TestCrossDupeIdempotency:
    def test_rerun_does_not_duplicate_flag(self, ctx):
        prior_source = ctx.vault_root / "prior_source.m4a"
        _gen_audio(prior_source, freq=440)
        h = _seed_prior_batch_hash(
            ctx, prior_source, str(ctx.alac_library / "Artist" / "Album" / "Existing.m4a")
        )

        incoming = ctx.inbox / "new_arrival.m4a"
        _gen_audio(incoming, freq=440)
        _register_hashed(ctx, incoming, h)

        first = CrossDupeStage().execute(ctx)
        assert first.files_changed == 1

        second = CrossDupeStage().execute(ctx)
        assert second.files_changed == 0  # already flagged, not re-flagged

        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM duplicates WHERE file_path = ?", (str(incoming),)
        ).fetchone()[0]
        assert count == 1  # exactly one row, not two


class TestCrossDupeDryRun:
    def test_dry_run_makes_no_db_changes(self, ctx_dry):
        prior_source = ctx_dry.vault_root / "prior_source.m4a"
        _gen_audio(prior_source, freq=440)
        h = _seed_prior_batch_hash(
            ctx_dry, prior_source, str(ctx_dry.alac_library / "Artist" / "Album" / "Existing.m4a")
        )

        incoming = ctx_dry.inbox / "new_arrival.m4a"
        _gen_audio(incoming, freq=440)
        _register_hashed(ctx_dry, incoming, h)

        result = CrossDupeStage().execute(ctx_dry)

        assert result.dry_run is True
        assert result.files_changed == 1
        assert any("CROSS-BATCH DUPLICATE" in n for n in result.notes)

        count = ctx_dry.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0]
        assert count == 0  # dry run must not write anything


class TestStaleHashIndexEntries:
    """The 2026-08-17/18 dupe cascade, pinned so it cannot recur.

    hash_index.db is append-only by design -- it has to survive a musaeus.db
    wipe between batches -- and nothing prunes an entry when the file it
    names is later moved. Treating a hit as proof a copy is held quarantined
    10,597 tracks as duplicates of files that no longer existed, including
    files that were duplicates of *themselves*. See scope doc section 4.17.
    """

    def test_stale_entry_does_not_flag(self, ctx):
        # Ledger says this audio was finalized; the named file is gone.
        prior_source = ctx.vault_root / "prior_source.m4a"
        _gen_audio(prior_source, freq=440)
        h = _seed_prior_batch_hash(
            ctx,
            prior_source,
            str(ctx.alac_library / "Artist" / "Album" / "Moved Away.m4a"),
            place_on_disk=False,
        )

        incoming = ctx.inbox / "new_arrival.m4a"
        _gen_audio(incoming, freq=440)
        _register_hashed(ctx, incoming, h)

        result = CrossDupeStage().execute(ctx)

        assert result.files_changed == 0
        assert ctx.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0] == 0
        assert any("stale" in n for n in result.notes)

    def test_file_is_not_its_own_duplicate(self, ctx):
        """The exact cascade: a file whose own path is what the ledger names.

        Once a finalized file had been relocated into quarantine, the ledger
        still pointed at where it used to be. Re-examined later, it matched
        its own entry and was quarantined as a duplicate of itself.
        """
        incoming = ctx.alac_library / "Artist" / "Album" / "Only Copy.m4a"
        _gen_audio(incoming, freq=660)
        h = _seed_prior_batch_hash(ctx, incoming, str(incoming))
        _register_hashed(ctx, incoming, h)

        result = CrossDupeStage().execute(ctx)

        assert result.files_changed == 0, "a file must never be its own duplicate"
        assert ctx.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0] == 0
        # ...and this is not a stale ledger: the indexed file is right there.
        # Saying otherwise would send someone pruning a healthy index.
        assert not any("stale" in n for n in result.notes)

    def test_live_twin_still_flags(self, ctx):
        """The guard must not cost real cross-batch detection."""
        prior_source = ctx.vault_root / "prior_source.m4a"
        _gen_audio(prior_source, freq=440)
        h = _seed_prior_batch_hash(
            ctx, prior_source, str(ctx.alac_library / "Artist" / "Album" / "Existing.m4a")
        )

        incoming = ctx.inbox / "new_arrival.m4a"
        _gen_audio(incoming, freq=440)
        _register_hashed(ctx, incoming, h)

        result = CrossDupeStage().execute(ctx)

        assert result.files_changed == 1
