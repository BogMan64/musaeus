"""Findings 1 and 4 of the review of PR #86 (2026-10-07).

1. TributeQuarantine runs in Act 1 and rescanned every CATALOGUED row --
   the filed masters too. On the live library of 2026-10-07 it would have
   moved Dean Martin's "I've Grown Accustomed To Her Face" out, because its
   album is "Sleep Warm", and a master put back by hand was caught again
   on the next Act 1. Act 1 now checks arrivals only; removing a filed
   master is a human decision (scripts/delete_reviewed_tracks.py).
   Separately, the album pattern \\brelax had no closing \\b, so "Relaxin'
   with the Miles Davis Quintet" matched.

4. The restore scripts both stages write put raw paths inside double
   quotes under `set -u`. "If I Had $1,000,000" stopped the script at that
   line; a path holding $(...) or backticks would have run as a command.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.dupe_resolver import DupeResolverStage
from musaeus.stages.tribute_quarantine import TributeQuarantineStage, is_junk


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )
    cfg.ensure_dirs()
    c = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    c.set("finalize_batch_date", "2026-01-15")
    return c


def _row(ctx: RunContext, path: Path, *, filed: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"FAKE AUDIO")
    upsert_archive(
        ctx.conn,
        {
            "file_path": str(path),
            "status": "CATALOGUED",
            "artist": "Dean Martin",
            "title": "I've Grown Accustomed To Her Face",
            "album": "Sleep Warm",
        },
    )
    if filed:
        ctx.conn.execute(
            "UPDATE archive SET finalized_at = datetime('now') WHERE file_path = ?", (str(path),)
        )
    ctx.conn.commit()
    return path


# ── 1: a filed master is not rescanned ──────────────────────────────────────


def test_a_filed_master_is_left_alone(ctx):
    master = _row(ctx, ctx.alac_library / "Dean Martin" / "Sleep Warm" / "song.m4a", filed=True)

    TributeQuarantineStage().run(ctx)

    assert master.is_file(), "Act 1 moved a filed master out of the library"
    status = ctx.conn.execute(
        "SELECT status FROM archive WHERE file_path = ?", (str(master),)
    ).fetchone()[0]
    assert status == "CATALOGUED"


def test_an_arrival_is_still_checked(ctx):
    arrival = _row(ctx, ctx.inbox / "Dean Martin" / "song.m4a", filed=False)

    TributeQuarantineStage().run(ctx)

    assert not arrival.exists(), "an arrival matching a pattern must still go to review"


def test_relax_needs_a_word_ending():
    assert is_junk("Miles Davis", "Oleo", "Relaxin' with the Miles Davis Quintet") == (False, "")
    assert is_junk("Sleepy Sounds Co", "Waves", "Relaxing Piano")[0]
    assert is_junk("Sleepy Sounds Co", "Waves", "Relaxation")[0]


# ── 4: restore scripts survive shell characters in paths ────────────────────

_NAMES = [
    "Barenaked Ladies - If I Had $1,000,000.m4a",
    "Ty Dolla $ign - Song.m4a",
    "$(touch PWNED) - Song.m4a",
    "`touch PWNED2` - Song.m4a",
    'Say "Hello" - Song.m4a',
]


@pytest.mark.parametrize("stage", [TributeQuarantineStage, DupeResolverStage])
def test_restore_script_puts_back_paths_with_shell_characters(ctx, tmp_path, stage):
    moves = []
    for n, name in enumerate(_NAMES):
        source = ctx.alac_library / "Artist" / f"Album {n}" / name
        destination = tmp_path / "REVIEW_HOLD" / f"{n}" / name
        destination.parent.mkdir(parents=True)
        destination.write_bytes(name.encode())
        move = {"source": str(source), "destination": str(destination)}
        if stage is TributeQuarantineStage:
            move["reason"] = "test"  # the resolver's manifest has no such column
        moves.append(move)

    _, script = stage()._write_manifest_and_restore_script(ctx, "2026-01-15", moves)
    run = subprocess.run(["bash", str(script)], cwd=tmp_path, capture_output=True, text=True)

    assert run.returncode == 0, run.stderr
    for m in moves:
        assert Path(m["source"]).read_bytes() == Path(m["source"]).name.encode()
    assert not list(tmp_path.rglob("PWNED*")), "a path ran as a command"
