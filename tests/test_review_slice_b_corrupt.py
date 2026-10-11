"""Review of slice B (#145), findings 1 and 2: CorruptStage.

1. A failure of the machine -- ffmpeg missing, a timeout, an I/O error -- came
   back from the decode as damage. The stage moved healthy masters to
   QUARANTINE and reported success, and a decode_ok=0 left that way (by
   deep_scan too) was later acted on without decoding again.
2. The decode ran without -nostdin: a key typed during a run ended it early
   with exit 0, and a damaged file read as clean.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus import deep_scan
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages import corrupt
from musaeus.stages.corrupt import CorruptStage

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not available",
)


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        db_path=tmp_path / "musaeus.db",
    )
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _master(ctx: RunContext, declared: float = 600.0) -> Path:
    """A healthy master. declared=600 for 3 s of audio: the size ratio
    accuses it, so the stage decodes it."""
    f = ctx.config.alac_archive / "Jazz" / "Count Basie" / "Album" / "track.m4a"
    f.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-c:a", "aac", str(f), "-y"],
        check=True, capture_output=True,
    )  # fmt: skip
    upsert_archive(
        ctx.conn,
        {"file_path": str(f), "status": "CATALOGUED", "codec": "aac",
         "duration": declared, "title": "track"},
    )  # fmt: skip
    ctx.conn.commit()
    return f


def _row(ctx, f):
    deep_scan.ensure_columns(ctx.conn)
    return ctx.conn.execute(
        "SELECT status, file_path, decode_ok FROM archive WHERE title='track'"
    ).fetchone()


def _no_ffmpeg(monkeypatch, tmp_path):
    empty = tmp_path / "empty_bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


def test_without_ffmpeg_a_healthy_master_stays(ctx, tmp_path, monkeypatch):
    f = _master(ctx)
    _no_ffmpeg(monkeypatch, tmp_path)
    result = CorruptStage()._scan(ctx, dry_run=False)
    monkeypatch.undo()
    row = _row(ctx, f)
    assert f.exists() and row["status"] == "CATALOGUED", "a healthy master was quarantined"
    assert row["decode_ok"] is None, "a machine failure was recorded as damage"
    assert result.errors, "a run that could not decode was reported clean"


def test_a_timeout_is_not_damage(ctx, monkeypatch):
    f = _master(ctx)
    real = subprocess.run

    def slow(cmd, *a, **k):
        if cmd and cmd[0] == "ffmpeg":
            raise subprocess.TimeoutExpired(cmd, 600)
        return real(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", slow)
    CorruptStage()._scan(ctx, dry_run=False)
    monkeypatch.undo()
    assert f.exists() and _row(ctx, f)["status"] == "CATALOGUED"


def test_an_old_failure_mark_is_decoded_again_before_anything_moves(ctx):
    f = _master(ctx, declared=3.0)  # not suspect by size
    deep_scan.ensure_columns(ctx.conn)
    ctx.conn.execute(
        "UPDATE archive SET decode_ok=0, decode_errors=1, decode_checked_at='2026-10-01'"
    )
    ctx.conn.commit()
    CorruptStage()._scan(ctx, dry_run=False)
    row = _row(ctx, f)
    assert f.exists() and row["status"] == "CATALOGUED", "quarantined on an old mark alone"
    assert row["decode_ok"] == 1


def test_deep_scan_records_nothing_when_ffmpeg_is_missing(ctx, tmp_path, monkeypatch):
    f = _master(ctx)
    _no_ffmpeg(monkeypatch, tmp_path)
    deep_scan.scan(ctx.conn, idle_only=False)
    monkeypatch.undo()
    assert _row(ctx, f)["decode_ok"] is None


def test_the_decode_never_reads_the_terminal(ctx, monkeypatch):
    f = _master(ctx)
    seen = {}
    real = subprocess.run

    def spy(cmd, *a, **k):
        if cmd and cmd[0] == "ffmpeg":
            seen.update(cmd=cmd, stdin=k.get("stdin"))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", spy)
    corrupt.ffmpeg_decode_check(f)
    assert "-nostdin" in seen["cmd"]
    assert seen["stdin"] is subprocess.DEVNULL


# ── Findings 3 and 5 ──────────────────────────────────────────────────────────


def _damaged(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
         "-c:a", "aac", "-movflags", "+faststart", str(path), "-y"],
        check=True, capture_output=True,
    )  # fmt: skip
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 3])  # header intact, audio cut
    return path


def test_a_quarantine_name_held_by_an_old_row_is_not_reused(ctx):
    deep_scan.ensure_columns(ctx.conn)  # as CorruptStage.run does first
    taken = ctx.vault_root / "QUARANTINE" / "corrupted" / "Bad.m4a"  # no file there
    upsert_archive(ctx.conn, {"file_path": str(taken), "status": "DELETED", "title": "old"})
    arrival = _damaged(ctx.config.inbox / "Bad.m4a")
    upsert_archive(ctx.conn, {"file_path": str(arrival), "status": "HASHED", "title": "new"})
    ctx.conn.commit()
    CorruptStage()._gate_new_arrivals(ctx, CorruptStage()._make_result(dry_run=False), False)
    rows = dict(ctx.conn.execute("SELECT title, file_path FROM archive").fetchall())
    status = dict(ctx.conn.execute("SELECT title, status FROM archive").fetchall())
    assert rows["old"] == str(taken) and not taken.exists(), "the old row now names a real file"
    assert status["new"] == "QUARANTINED", "the arrival's row did not follow its file"
    assert Path(rows["new"]).exists() and Path(rows["new"]) != taken


def test_verify_effect_sees_a_quarantine_this_run_made(ctx):
    bad = _damaged(ctx.config.alac_archive / "Rock" / "X" / "Y" / "broken.m4a")
    upsert_archive(
        ctx.conn,
        {"file_path": str(bad), "status": "CATALOGUED", "codec": "aac",
         "duration": 30.0, "title": "broken"},
    )  # fmt: skip
    ctx.conn.commit()
    stage = CorruptStage()
    result = stage._scan(ctx, dry_run=False)
    row = ctx.conn.execute("SELECT status, file_path FROM archive").fetchone()
    assert row["status"] == "QUARANTINED"
    assert stage.verify_effect(ctx, result) == []
    Path(row["file_path"]).unlink()  # the quarantined file vanishes
    assert stage.verify_effect(ctx, result), "the check never looked at the row"


# ── Pre-merge review of #162 ──────────────────────────────────────────────────


def test_a_long_path_does_not_hide_the_machine_failure(tmp_path):
    gone = tmp_path / ("x" * 190) / ("y" * 60) / "song.m4a"  # never created
    with pytest.raises(corrupt.DecodeUnavailable):
        corrupt.ffmpeg_decode_check(gone)


def test_a_stopped_ffmpeg_is_not_a_verdict(tmp_path, monkeypatch):
    f = tmp_path / "a.m4a"
    f.write_bytes(b"x")
    real = subprocess.run

    def stopped(cmd, *a, **k):
        if cmd and cmd[0] == "ffmpeg":
            return subprocess.CompletedProcess(cmd, 255, "", "")  # SIGTERM caught
        return real(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", stopped)
    with pytest.raises(corrupt.DecodeUnavailable):
        corrupt.ffmpeg_decode_check(f)


def test_a_file_named_like_a_machine_failure_is_still_judged(tmp_path):
    f = _damaged(tmp_path / "Permission Denied.m4a")
    ok, why = corrupt.ffmpeg_decode_check(f)
    assert not ok and why
