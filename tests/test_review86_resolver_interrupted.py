"""Review of #86, finding 10 (2026-10-07): an interrupted resolver run left files
moved while their rows rolled back -- one commit, after every group -- and no
manifest or restore script, which were written only at the end. Each move is now
committed as it completes, and its restore line is on disk before the file moves.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages import dupe_resolver
from musaeus.stages.dupe_resolver import DupeResolverStage


@pytest.fixture
def ctx(tmp_path):
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    cfg.meta_dir.mkdir(parents=True)
    c = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    c.set("finalize_batch_date", "2026-01-15")
    return c


def _row(ctx, path: Path, **kw) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"X" * kw.pop("size_bytes", 300))
    kw.setdefault("audio_hash", "pcm:" + path.name)
    upsert_archive(ctx.conn, {"file_path": str(path), "status": "CATALOGUED", **kw})
    ctx.conn.commit()
    return path


def _dup(ctx, gid, path):
    ctx.conn.execute(
        "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, run_id, audio_hash) "
        "VALUES (?, ?, 'NEAR', 1.0, ?, ?)", (gid, str(path), ctx.run_id, "pcm:" + Path(path).name))  # fmt: skip
    ctx.conn.commit()


def test_a_killed_run_leaves_its_moves_recorded_and_restorable(ctx):
    pairs = []
    for n in range(3):
        keep = _row(ctx, ctx.alac_library / f"S{n}" / "keep.m4a", artist=f"S{n}", album="B",
                    title="T", codec="alac", bitrate=900_000)  # fmt: skip
        lose = _row(ctx, ctx.alac_library / f"S{n}" / "lose.m4a", artist=f"S{n}", album="B",
                    title="T", codec="aac", bitrate=256_000)  # fmt: skip
        _dup(ctx, f"near_{n}aaaaaaa", keep)
        _dup(ctx, f"near_{n}aaaaaaa", lose)
        pairs.append((keep, lose))

    real_move, calls = dupe_resolver.shutil.move, []

    def killed_on_the_second(src, dst):
        calls.append(src)
        if len(calls) == 3:
            raise KeyboardInterrupt  # the run is killed mid-way
        return real_move(src, dst)

    dupe_resolver.shutil.move = killed_on_the_second
    try:
        with pytest.raises(KeyboardInterrupt):
            DupeResolverStage().run(ctx)
    finally:
        dupe_resolver.shutil.move = real_move

    # The second move: the first group's commits by luck (its savepoint was the
    # outermost transaction); after it the keeper mark opens one, and every
    # later move waited for the single commit at the end.
    moved_from = Path(calls[1])
    assert not moved_from.exists(), "the second move happened"
    fresh = sqlite3.connect(ctx.config.db_path)  # what survives the killed process
    status, path = fresh.execute(
        "SELECT status, file_path FROM archive WHERE file_path NOT LIKE '%keep.m4a' "
        "AND (file_path = ? OR status = 'DUPE_REVIEW')", (str(moved_from),)).fetchone()  # fmt: skip
    assert status == "DUPE_REVIEW" and Path(path).exists(), "the moved file's row rolled back"

    scripts = list(ctx.config.dupes_review_dir.rglob("restore_*.sh"))
    assert scripts, "no restore script for the moves already made"
    subprocess.run(["bash", str(scripts[0])], check=True)
    assert moved_from.exists(), "the restore script did not bring the file back"
