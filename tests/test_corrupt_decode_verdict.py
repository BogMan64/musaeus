"""The size ratio accuses; only the decode convicts.

CorruptStage physically MOVES files into QUARANTINE/corrupted/. Until
2026-09-02 it did so on `check_file()` alone -- a filesize-against-duration
heuristic that describes a file's shape and never reads its contents.

deep_scan.py had already measured what that heuristic is worth on this
library: it flagged 418 files, of which 2 were damaged and 91 were
undamaged Bing Crosby, Count Basie and Billie Holiday -- old mono
recordings that genuinely compress to ~13% of PCM. Acting on the ratio
alone meant physically removing ~91 good masters.

ffmpeg_decode_check() had sat in this same module since the day before
deep_scan was written, correct and never called from the stage. Now the
suspects get decoded, and only a failed decode is a verdict.
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


def _tone(path: Path, seconds: int = 3) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:a", "aac", "-movflags", "+faststart", str(path), "-y"],
        check=True, capture_output=True,
    )
    return path


def _register(ctx: RunContext, path: Path, declared_duration: float) -> None:
    """Register the file claiming a much longer duration than its bytes.

    That is what trips the size-ratio heuristic -- exactly the shape of a
    genuine old mono recording that compresses unusually well.
    """
    upsert_archive(ctx.conn, {
        "file_path": str(path), "status": "CATALOGUED",
        "codec": "aac", "duration": declared_duration, "title": path.stem,
    })
    ctx.conn.commit()


def test_a_file_the_ratio_accuses_but_the_decode_acquits_is_left_alone(ctx, cfg) -> None:
    """The 91-undamaged-masters case. This is the whole point."""
    f = _tone(cfg.alac_library / "Count Basie" / "Album" / "track.m4a")
    _register(ctx, f, declared_duration=600.0)   # 3s of audio claiming 10 minutes

    result = CorruptStage()._scan(ctx, dry_run=False)

    assert f.exists(), "an intact file must never be quarantined on a size ratio"
    assert not (cfg.vault_root / "QUARANTINE" / "corrupted" / f.name).exists()
    assert any("decoded cleanly" in n for n in result.notes), result.notes


def test_the_clearing_is_reported_not_silent(ctx, cfg) -> None:
    f = _tone(cfg.alac_library / "A" / "B" / "t.m4a")
    _register(ctx, f, declared_duration=600.0)
    result = CorruptStage()._scan(ctx, dry_run=False)
    assert any("flagged by size ratio" in n for n in result.notes), result.notes


def test_a_truncated_file_is_still_caught(ctx, cfg) -> None:
    """The decode must remain a real verdict, not a rubber stamp."""
    f = _tone(cfg.alac_library / "C" / "D" / "broken.m4a", seconds=30)
    data = f.read_bytes()
    f.write_bytes(data[: len(data) // 3])        # header intact, audio cut
    _register(ctx, f, declared_duration=30.0)

    CorruptStage()._scan(ctx, dry_run=False)

    assert not f.exists(), "a file that fails to decode must be quarantined"
    assert (cfg.vault_root / "QUARANTINE" / "corrupted" / "broken.m4a").exists()


def test_the_decode_result_is_recorded_so_deep_scan_need_not_repeat_it(ctx, cfg) -> None:
    f = _tone(cfg.alac_library / "E" / "F" / "t.m4a")
    _register(ctx, f, declared_duration=600.0)
    CorruptStage()._scan(ctx, dry_run=False)
    row = ctx.conn.execute(
        "SELECT decode_checked_at, decode_ok FROM archive WHERE file_path = ?", (str(f),)
    ).fetchone()
    assert row["decode_checked_at"] is not None
    assert row["decode_ok"] == 1


def test_a_dry_run_decodes_but_writes_nothing(ctx, cfg) -> None:
    """A preview that skipped the decode would report the old, wrong
    verdict -- the preview has to mean what the run means."""
    f = _tone(cfg.alac_library / "G" / "H" / "t.m4a")
    _register(ctx, f, declared_duration=600.0)

    result = CorruptStage()._scan(ctx, dry_run=True)

    assert f.exists()
    assert any("decoded cleanly" in n for n in result.notes), result.notes
    # Not even the column: adding one is an ALTER TABLE, and the planner's
    # safety statement promises a preview changes no managed state.
    cols = {r[1] for r in ctx.conn.execute("PRAGMA table_info(archive)").fetchall()}
    if "decode_checked_at" in cols:
        row = ctx.conn.execute(
            "SELECT decode_checked_at FROM archive WHERE file_path = ?", (str(f),)
        ).fetchone()
        assert row["decode_checked_at"] is None, "dry run must not write"


def test_ffprobe_duration_no_longer_claims_to_decode() -> None:
    """It reads metadata. In MP4 both the stream and container durations
    come from the same moov atom and survive truncation intact."""
    from musaeus.stages.corrupt import ffprobe_duration

    first_line = (ffprobe_duration.__doc__ or "").strip().splitlines()[0].lower()
    assert "metadata" in first_line, first_line
    assert "actual decoded duration" not in first_line, first_line
