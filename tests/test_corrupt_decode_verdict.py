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
from musaeus.deep_scan import ensure_columns
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


# ── The discovery gap: right-sized files with damage in the stream ────────────
#
# check_file() cannot see this class by construction -- it compares bytes
# to declared duration. Billy Joel, The Who, Billy Ocean, Bachman-Turner
# Overdrive and Tower of Power were all the RIGHT SIZE for their declared
# duration and NONE were ever flagged as suspects. Real numbers measured
# against the live library 2026-09-02: 81.3 MB for 363 declared seconds,
# 168.0 MB for 299 -- unremarkable ratios, genuinely damaged audio.


def test_a_right_sized_never_checked_file_still_gets_decoded(ctx, cfg) -> None:
    """The actual capability this exists for: NOT flagged as a size-ratio
    suspect, NEVER decode-checked by anything -- still gets a real answer."""
    f = _tone(cfg.alac_library / "X" / "Y" / "t.m4a", seconds=50)
    _register(ctx, f, declared_duration=50.0)  # correct size for its duration
    from musaeus.stages.corrupt import check_file

    is_suspect, _ = check_file(f, "aac", 50.0)
    assert not is_suspect, "fixture must not be a size-ratio suspect"

    CorruptStage()._scan(ctx, dry_run=False)

    row = ctx.conn.execute(
        "SELECT decode_checked_at, decode_ok FROM archive WHERE file_path = ?", (str(f),)
    ).fetchone()
    assert row["decode_checked_at"] is not None
    assert row["decode_ok"] == 1


def test_a_right_sized_damaged_file_is_caught_and_quarantined(ctx, cfg) -> None:
    """Reproduces the actual shape, not a byte-truncation.

    Cutting bytes off the end (as test_duration.py's fixture does) SHRINKS
    the file relative to its declared duration -- exactly what check_file's
    size ratio is designed to catch, so it is not the case this test needs.
    The real corrupt masters were the RIGHT SIZE: intact container, damaged
    audio data. Reproduced here by zeroing a middle slice of the file in
    place -- same total bytes, same header, genuinely undecodable. Verified
    directly: ffprobe still reports the full declared duration; only a
    decode shows the damage ("channel element 0.0 is not allocated").
    """
    f = _tone(cfg.alac_library / "P" / "Q" / "broken.m4a", seconds=50)
    data = bytearray(f.read_bytes())
    lo, hi = int(len(data) * 0.4), int(len(data) * 0.6)
    for i in range(lo, hi):
        data[i] = 0
    f.write_bytes(bytes(data))
    from musaeus.stages.corrupt import check_file

    is_suspect, _ = check_file(f, "aac", 50.0)
    assert not is_suspect, "a same-size corruption must not trip the size ratio"
    _register(ctx, f, declared_duration=50.0)

    result = CorruptStage()._scan(ctx, dry_run=False)

    assert not f.exists(), "the decode must catch it and quarantine it"
    assert (cfg.vault_root / "QUARANTINE" / "corrupted" / "broken.m4a").exists()
    assert result.files_changed == 1


def test_already_checked_intact_files_are_never_re_decoded(ctx, cfg) -> None:
    """decode_checked_at already set, decode_ok=1: nothing to discover.
    Re-decoding it would be pure waste on every single run forever."""
    f = _tone(cfg.alac_library / "M" / "N" / "t.m4a", seconds=50)
    _register(ctx, f, declared_duration=50.0)
    ensure_columns(ctx.conn)
    ctx.conn.execute(
        "UPDATE archive SET decode_checked_at = datetime('now'), decode_ok = 1 "
        "WHERE file_path = ?",
        (str(f),),
    )
    ctx.conn.commit()

    import musaeus.stages.corrupt as corrupt_mod
    calls = {"n": 0}
    real = corrupt_mod.ffmpeg_decode_check

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    corrupt_mod.ffmpeg_decode_check = counting
    try:
        CorruptStage()._scan(ctx, dry_run=False)
    finally:
        corrupt_mod.ffmpeg_decode_check = real

    assert calls["n"] == 0, "an already-clean, already-checked file must not be re-decoded"


def test_a_row_deep_scan_already_marked_bad_is_quarantined_without_a_new_decode(ctx, cfg) -> None:
    """deep_scan and CorruptStage share decode_ok/decode_checked_at rather
    than each keeping their own. A file deep_scan already proved bad must
    be acted on here without paying for a second decode of a file already
    known damaged."""
    f = cfg.alac_library / "D" / "E" / "already_bad.m4a"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"not real audio, but it does not matter -- decode_ok is pre-set")
    _register(ctx, f, declared_duration=200.0)
    ensure_columns(ctx.conn)
    ctx.conn.execute(
        "UPDATE archive SET decode_checked_at = datetime('now'), decode_ok = 0, "
        "decode_errors = 1 WHERE file_path = ?",
        (str(f),),
    )
    ctx.conn.commit()

    import musaeus.stages.corrupt as corrupt_mod
    calls = {"n": 0}
    real = corrupt_mod.ffmpeg_decode_check

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    corrupt_mod.ffmpeg_decode_check = counting
    try:
        result = CorruptStage()._scan(ctx, dry_run=False)
    finally:
        corrupt_mod.ffmpeg_decode_check = real

    assert calls["n"] == 0, "must trust deep_scan's prior finding, not re-decode"
    assert not f.exists(), "a row already known bad must still be quarantined"
    assert result.files_changed == 1


def test_the_new_arrival_decode_budget_is_bounded(ctx, cfg) -> None:
    """Unbounded discovery decoding would repeat the AcousticID mistake --
    a full-library migration wired unconditionally into every run held
    the DB write lock for 21 hours. With the budget set to 1, a second
    never-checked file must be left unattempted this run -- ready for
    deep_scan or a later run, not blocking Act 1 for however large the
    backlog happens to be.
    """
    f1 = _tone(cfg.alac_library / "A1" / "t.m4a", seconds=50)
    f2 = _tone(cfg.alac_library / "A2" / "t.m4a", seconds=50)
    _register(ctx, f1, declared_duration=50.0)
    _register(ctx, f2, declared_duration=50.0)

    stage = CorruptStage()
    stage.NEW_ARRIVAL_DECODE_BUDGET = 1
    stage._scan(ctx, dry_run=False)

    checked = [
        ctx.conn.execute(
            "SELECT decode_checked_at FROM archive WHERE file_path = ?", (str(f),)
        ).fetchone()["decode_checked_at"] is not None
        for f in (f1, f2)
    ]
    assert sum(checked) == 1, (
        f"budget=1 must check exactly one of the two never-checked files, got {checked}"
    )


def test_the_default_budget_covers_an_ordinary_batch(ctx, cfg) -> None:
    """The default (200) must not be so tight it fails to check a normal
    handful of new arrivals -- sized against the ~1.46s/file measured
    2026-09-02, where 200 files costs about 5 minutes, not hours."""
    files = [_tone(cfg.alac_library / f"B{i}" / "t.m4a", seconds=50) for i in range(5)]
    for f in files:
        _register(ctx, f, declared_duration=50.0)

    CorruptStage()._scan(ctx, dry_run=False)

    for f in files:
        row = ctx.conn.execute(
            "SELECT decode_checked_at FROM archive WHERE file_path = ?", (str(f),)
        ).fetchone()
        assert row["decode_checked_at"] is not None
