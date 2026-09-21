"""A damaged file must never reach the library in the first place.

`ffmpeg_decode_check` was called from exactly one place: `_scan()`, whose
query is `WHERE status = 'CATALOGUED'`. A new arrival is PENDING/HASHED until
FinalizeStage promotes it, and CorruptStage runs BEFORE Finalize -- so on the
run that ingested a file, nothing ever decoded it. It became eligible only on
the NEXT run, and then only if the size heuristic flagged it or it won one of
NEW_ARRIVAL_DECODE_BUDGET slots.

That is how five truncated files reached the library as masters on
2026-09-21. All five were right-sized, so the shape heuristic could not see
them, and metadata cannot see truncation: in MP4 both durations live in the
`moov` atom, written before the audio, so a file cut short still reports its
full length. Only a decode knows.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.corrupt import CorruptStage

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
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _tone(path: Path, seconds: int = 5) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
         f"sine=frequency=440:duration={seconds}", "-c:a", "alac",
         "-movflags", "+faststart", str(path), "-y"],
        check=True, capture_output=True,
    )
    return path


def _truncate(path: Path, keep: float = 0.55) -> Path:
    """Cut the audio short but leave the moov atom intact.

    +faststart puts moov first, so the declared duration survives the cut --
    the file still claims its full length. This is the real failure shape.
    """
    data = path.read_bytes()
    path.write_bytes(data[: int(len(data) * keep)])
    return path


def _arrival(ctx: RunContext, path: Path, status: str = "HASHED") -> None:
    upsert_archive(
        ctx.conn,
        {"file_path": str(path), "status": status, "codec": "alac",
         "duration": 5.0, "title": path.stem, "artist": "Test"},
    )
    ctx.conn.commit()


def _status(ctx: RunContext, title: str) -> str | None:
    row = ctx.conn.execute(
        "SELECT status FROM archive WHERE title = ?", (title,)
    ).fetchone()
    return row["status"] if row else None


def test_a_truncated_arrival_is_refused_before_it_can_be_catalogued(ctx, cfg) -> None:
    f = _truncate(_tone(cfg.inbox / "broken.m4a"))
    _arrival(ctx, f)

    CorruptStage().run(ctx)

    assert _status(ctx, "broken") == "QUARANTINED", (
        "a file that does not decode must not be left eligible for Finalize"
    )
    assert not f.exists(), "the damaged file should have been moved out of INBOX"
    assert (cfg.vault_root / "QUARANTINE" / "corrupted" / "broken.m4a").exists()


def test_an_intact_arrival_passes_through_untouched(ctx, cfg) -> None:
    f = _tone(cfg.inbox / "good.m4a")
    _arrival(ctx, f)

    CorruptStage().run(ctx)

    assert _status(ctx, "good") == "HASHED", "an intact arrival must not be gated"
    assert f.exists()


def test_the_gate_records_its_verdict_so_the_audit_need_not_redo_it(ctx, cfg) -> None:
    f = _tone(cfg.inbox / "recorded.m4a")
    _arrival(ctx, f)

    CorruptStage().run(ctx)

    row = ctx.conn.execute(
        "SELECT decode_ok, decode_checked_at FROM archive WHERE title = 'recorded'"
    ).fetchone()
    assert row["decode_ok"] == 1
    assert row["decode_checked_at"] is not None


def test_every_pre_finalize_status_is_gated(ctx, cfg) -> None:
    """PENDING and HASHED both precede Finalize; neither may slip past."""
    for status in CorruptStage.ARRIVAL_STATUSES:
        f = _truncate(_tone(cfg.inbox / f"broken_{status}.m4a"))
        _arrival(ctx, f, status=status)

    CorruptStage().run(ctx)

    for status in CorruptStage.ARRIVAL_STATUSES:
        assert _status(ctx, f"broken_{status}") == "QUARANTINED", status
