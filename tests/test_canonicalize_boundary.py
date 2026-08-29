"""
Canonicalize's destructive step must be recoverable.

canonicalize disposes of the pre-conversion original once the archive row
points at the verified STAGING copy. Two things were wrong with that.

DURABILITY. The disposal was per-file, but the DB write that justifies it
was not durable until ctx.conn.commit(), which run() called only every
_COMMIT_EVERY (25) rows. open_db() connects with sqlite3's default
deferred isolation -- not autocommit, unlike musaeus/state/migrator.py
which sets isolation_level=None explicitly where it wants that. So up to
24 rows could have had their original deleted while the UPDATE naming the
replacement sat in an open transaction. A kill in that window discarded
it: original gone, converted audio an unattributed orphan in STAGING, and
the archive row still naming the deleted path with canonicalized_at NULL,
which the next run errors as missing forever.

RECOVERABILITY. The disposal was an unlink, so even a committed run could
not be undone. Unlike finalize, canonicalize rewrites the audio stream,
so tag-capture checkpointing cannot describe the original -- the bytes
themselves have to survive.

Both are fixed by committing per row before disposing, and by disposing
through MutationBoundary.quarantine, whose contract is a move and never a
delete.

Uses real ffmpeg audio for the same reason test_canonicalize.py does: a
mock would hide exactly the class of bug these tests exist to catch.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.canonicalize import CanonicalizeStage

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


def _gen_flac(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:a", "flac", str(path)],
        capture_output=True, check=True,
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _register(ctx: RunContext, path: Path) -> None:
    upsert_archive(ctx.conn, {
        "file_path": str(path), "filename": path.name, "ext": ".flac",
        "status": "CATALOGUED", "codec": "flac", "artist": "Test Artist",
        "title": "Test Title", "bitrate": 900000, "sample_rate": 44100,
        "channels": 1, "duration": 2.0,
    })
    ctx.conn.commit()


def _setup(cfg: MusicConfig):
    cfg.ensure_dirs()
    conn = open_db(cfg.db_path)
    ctx = RunContext.new(cfg, conn, dry_run=False)
    source = cfg.inbox / "Test Artist" / "track.flac"
    _gen_flac(source)
    _register(ctx, source)
    return conn, ctx, source


def _quarantined_files(cfg: MusicConfig) -> list[Path]:
    recovery = cfg.runs_root / "recovery"
    if not recovery.exists():
        return []
    return [p for p in recovery.rglob("quarantine/*/*") if p.is_file()]


class TestOriginalIsPreservedNotDeleted:
    def test_original_moves_to_quarantine_with_bytes_intact(self, cfg: MusicConfig):
        conn, ctx, source = _setup(cfg)
        before = _sha(source)

        result = CanonicalizeStage().run(ctx)
        conn.close()

        assert not source.exists(), "the original should no longer sit in INBOX"
        held = _quarantined_files(cfg)
        assert len(held) == 1, f"expected the original to be held, found {held}"
        assert _sha(held[0]) == before, "quarantined bytes differ from the original"
        assert any("recovery boundary: checkpoint" in n for n in result.notes)


class TestCrashBetweenDisposalAndCommit:
    def test_row_is_durable_before_the_original_is_disposed_of(self, cfg: MusicConfig):
        """Kill the process at the instant of disposal.

        Everything sequenced before that instant happened; nothing after
        it did. The invariant is that the archive row naming the staged
        replacement is already durable by then -- so a fresh process finds
        a row pointing at a file that exists, whether or not the original
        was successfully disposed of.

        Against the pre-fix code this fails: the commit came only every
        _COMMIT_EVERY rows, so the unlink ran first and the crash discarded
        the row that explained it.
        """
        conn, ctx, source = _setup(cfg)

        def _die(*_a, **_k):
            raise KeyboardInterrupt("process killed mid-disposal")

        # Patch the disposal itself, leaving everything before it intact.
        import musaeus.safety.mutation as mutation_mod

        original_quarantine = mutation_mod.MutationBoundary.quarantine
        mutation_mod.MutationBoundary.quarantine = _die  # type: ignore[method-assign]
        try:
            with pytest.raises(KeyboardInterrupt):
                CanonicalizeStage().run(ctx)
        finally:
            mutation_mod.MutationBoundary.quarantine = original_quarantine  # type: ignore[method-assign]
            conn.close()  # discards any still-open transaction, as a crash would

        conn2 = open_db(cfg.db_path)
        row = conn2.execute(
            "SELECT file_path, canonicalized_at FROM archive"
        ).fetchone()
        conn2.close()

        assert row["canonicalized_at"] is not None, (
            "the original was disposed of while the row explaining it was "
            f"still uncommitted; a fresh process sees file_path={row['file_path']!r}"
        )
        assert Path(row["file_path"]).exists(), (
            f"archive row names {row['file_path']!r}, which is not on disk"
        )
        # Disposal never ran, so the original is simply still there. A
        # leftover original is recoverable; a lost one is not.
        assert source.exists(), "original should be untouched when disposal never ran"


class TestEscapeHatch:
    def test_disabled_boundary_says_so_in_the_result(self, cfg: MusicConfig, monkeypatch):
        monkeypatch.setenv(CanonicalizeStage.CHECKPOINT_ENV, "0")
        conn, ctx, source = _setup(cfg)

        result = CanonicalizeStage().run(ctx)
        conn.close()

        assert any(
            "recovery boundary: DISABLED" in n for n in result.notes
        ), f"a run without a boundary must announce itself; notes were {result.notes}"
        assert not source.exists()
        assert not _quarantined_files(cfg), "disabled boundary should not hold anything"
