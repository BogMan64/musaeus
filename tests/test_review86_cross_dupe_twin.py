"""Review of #86, finding 2 (2026-10-07): CrossDupe counted any file at a ledger
path as the master's twin when the catalogue had no row for it, and never
checked its audio. A different recording left there -- an untracked "(2)" from
an interrupted Finalize, say -- made the incoming file a "duplicate", and the
resolver then moved the only copy of it. Without a catalogue row, the file's
own audio now decides.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, open_hash_index, record_finalized_hash, upsert_archive
from musaeus.hasher import audio_hash
from musaeus.stages.cross_dupe import CrossDupeStage

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")


def _song(path: Path, freq: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"sine=f={freq}:d=1", "-c:a", "alac", str(path)], check=True)  # fmt: skip
    return path


@pytest.fixture
def ctx(tmp_path):
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    cfg.ensure_dirs()
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _incoming_and_ledger(ctx, at_ledger_path_freq: int) -> Path:
    incoming = _song(ctx.inbox / "song.m4a", 440)
    ah = audio_hash(incoming)
    upsert_archive(ctx.conn, {"file_path": str(incoming), "status": "CATALOGUED", "audio_hash": ah})
    ctx.conn.commit()
    filed = _song(ctx.alac_library / "A" / "song (2).m4a", at_ledger_path_freq)  # no catalogue row
    ledger = open_hash_index(ctx.config.hash_index_path)
    record_finalized_hash(ledger, ah, str(filed))
    ledger.commit()
    ledger.close()
    return incoming


def _flagged(ctx) -> int:
    return ctx.conn.execute(
        "SELECT COUNT(*) FROM duplicates WHERE duplicate_type = 'CROSS_BATCH'"
    ).fetchone()[0]


def test_a_different_recording_at_the_ledger_path_is_not_a_twin(ctx):
    _incoming_and_ledger(ctx, at_ledger_path_freq=880)

    CrossDupeStage().execute(ctx)

    assert _flagged(ctx) == 0, "the only copy was flagged as a duplicate of a different song"


def test_the_same_recording_at_the_ledger_path_is_a_twin(ctx):
    _incoming_and_ledger(ctx, at_ledger_path_freq=440)

    CrossDupeStage().execute(ctx)

    assert _flagged(ctx) == 1
