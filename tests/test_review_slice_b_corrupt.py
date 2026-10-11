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
