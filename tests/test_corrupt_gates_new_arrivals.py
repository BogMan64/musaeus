"""A damaged file must never reach the library in the first place.

`ffmpeg_decode_check` was called from exactly one place: `_scan()`, which
decodes only NEW_ARRIVAL_DECODE_BUDGET never-checked rows a run (beyond what
the size heuristic flags). CorruptStage runs BEFORE Finalize, so on the run
that ingested a file nothing was sure to decode it.

The gate built for that asked for PENDING/HASHED rows only -- but Scholar runs
before Corrupt and catalogues every arrival, so in a real run the gate saw
almost nothing: on 2026-10-11, 8,118 of 8,819 masters had never been decoded
(found tracing Grey's code review, 2026-10-11, finding 2). An arrival is any
row Finalize has not filed yet.

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
from musaeus.stages import corrupt as corrupt_mod
from musaeus.stages.corrupt import CorruptStage, DecodeUnavailable

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
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-c:a",
            "alac",
            "-movflags",
            "+faststart",
            str(path),
            "-y",
        ],
        check=True,
        capture_output=True,
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
        {
            "file_path": str(path),
            "status": status,
            "codec": "alac",
            "duration": 5.0,
            "title": path.stem,
            "artist": "Test",
        },
    )
    ctx.conn.commit()


def _status(ctx: RunContext, title: str) -> str | None:
    row = ctx.conn.execute("SELECT status FROM archive WHERE title = ?", (title,)).fetchone()
    return row["status"] if row else None


def test_a_truncated_arrival_is_refused_before_it_can_be_catalogued(ctx, cfg) -> None:
    f = _truncate(_tone(cfg.inbox / "broken.m4a"))
    _arrival(ctx, f)

    CorruptStage().run(ctx)

    assert _status(ctx, "broken") == "QUARANTINED", (
        "a file that does not decode must not be left eligible for Finalize"
    )
    assert not f.exists(), "the damaged file should have been moved out of INBOX"
    assert (cfg.quarantine / "corrupted" / "broken.m4a").exists()


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


def _catalogued_arrival(ctx: RunContext, path: Path) -> None:
    """The state Scholar leaves an arrival in: CATALOGUED, not yet filed."""
    _arrival(ctx, path, status="CATALOGUED")


def test_an_arrival_scholar_has_already_catalogued_is_gated(ctx, cfg, monkeypatch) -> None:
    # The real pipeline's order: Scholar has catalogued it, Finalize has not
    # filed it.
    f = _truncate(_tone(cfg.staging / "broken.m4a"))
    _catalogued_arrival(ctx, f)

    # The gate alone: a test tone is small enough that _scan's size
    # heuristic would catch it, which the real failure shape (right-sized
    # files) never is.
    stage = CorruptStage()
    stage._gate_new_arrivals(ctx, stage._make_result(), dry_run=False)

    assert _status(ctx, "broken") == "QUARANTINED"
    assert not f.exists()


def test_a_filed_master_is_not_gated_again(ctx, cfg, monkeypatch) -> None:
    f = _tone(cfg.alac_library / "master.m4a")
    _catalogued_arrival(ctx, f)
    ctx.conn.execute("UPDATE archive SET finalized_at = datetime('now')")
    ctx.conn.commit()
    monkeypatch.setattr(CorruptStage, "NEW_ARRIVAL_DECODE_BUDGET", 0)
    decoded: list[Path] = []
    monkeypatch.setattr(
        corrupt_mod, "ffmpeg_decode_check", lambda p, seconds=0: (decoded.append(p), (True, ""))[1]
    )

    # The gate alone: _scan's own sweep of the library may decode a master,
    # and that is its job, not the gate's.
    stage = CorruptStage()
    stage._gate_new_arrivals(ctx, stage._make_result(), dry_run=False)

    assert decoded == [], "the arrival gate re-decoded a master Finalize had filed"


def test_an_arrival_already_decoded_clean_is_not_decoded_twice(ctx, cfg, monkeypatch) -> None:
    f = _tone(cfg.staging / "good.m4a")
    _catalogued_arrival(ctx, f)
    calls: list[Path] = []
    real = corrupt_mod.ffmpeg_decode_check

    def counting(p, seconds=0):
        calls.append(p)
        return real(p, seconds=seconds)

    monkeypatch.setattr(corrupt_mod, "ffmpeg_decode_check", counting)

    CorruptStage().run(ctx)

    assert calls == [f], "_scan's budget decoded it; the gate must not decode it again"


def test_a_decode_that_could_not_be_done_fails_the_stage(ctx, cfg, monkeypatch) -> None:
    # A timeout on a slow drive: nothing is known about the file. It is not
    # quarantined (it may be fine) and the stage does not report success.
    f = _tone(cfg.staging / "slow.m4a")
    _arrival(ctx, f)
    monkeypatch.setattr(CorruptStage, "NEW_ARRIVAL_DECODE_BUDGET", 0)

    def timed_out(p, seconds=0):
        raise DecodeUnavailable("decode timed out after 600s")

    monkeypatch.setattr(corrupt_mod, "ffmpeg_decode_check", timed_out)

    result = CorruptStage().run(ctx)

    assert result.success is False
    assert any("arrival not decoded" in e for e in result.errors)
    assert _status(ctx, "slow") == "HASHED" and f.exists()
    row = ctx.conn.execute("SELECT decode_ok FROM archive WHERE title = 'slow'").fetchone()
    assert row["decode_ok"] is None, "an undone decode must not be recorded as a verdict"


def test_quarantine_follows_the_configured_folder(tmp_path) -> None:
    # Grey's code review, 2026-10-11, finding 9: the stage wrote to
    # vault_root/QUARANTINE and its own check looked in cfg.quarantine.
    cfg = MusicConfig(
        vault_root=tmp_path / "vault",
        inbox=tmp_path / "vault" / "INBOX",
        staging=tmp_path / "vault" / "STAGING",
        quarantine=tmp_path / "elsewhere" / "Q",
        runs_root=tmp_path / "vault" / "RUNS",
        meta_dir=tmp_path / "vault" / "MetaData",
        alac_library=tmp_path / "vault" / "ALAC-Library",
        db_path=tmp_path / "vault" / "musaeus.db",
    )
    cfg.vault_root.mkdir(parents=True)
    ctx = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    f = _truncate(_tone(cfg.inbox / "broken.m4a"))
    _arrival(ctx, f)

    stage = CorruptStage()
    result = stage.run(ctx)

    assert (cfg.quarantine / "corrupted" / "broken.m4a").exists()
    assert not (cfg.vault_root / "QUARANTINE").exists()
    assert stage.verify_effect(ctx, result) == []


def test_a_library_file_is_never_taken_as_an_arrival(ctx, cfg) -> None:
    """Pre-merge review of #179: an unfinalized row on an ALAC_Library file
    would have been decoded and, if bad, quarantined out of the library."""
    f = _truncate(_tone(cfg.alac_library / "Rock" / "edition.m4a"))
    _catalogued_arrival(ctx, f)

    stage = CorruptStage()
    stage._gate_new_arrivals(ctx, stage._make_result(), dry_run=False)

    assert f.exists() and _status(ctx, "edition") == "CATALOGUED"
