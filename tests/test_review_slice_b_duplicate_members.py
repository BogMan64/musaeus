"""Review of slice B (#145), finding 7: duplicates has no UNIQUE key, so the
stages' `INSERT OR IGNORE` never ignored anything. An EXACT group is named
after its recording; each new copy of a recording added the library master
to its old group again beside the earlier row (117 doubled pairs live)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.sentinel import SentinelStage

HASH = "c" * 64
GROUP = f"dup_{HASH[:12]}"


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


@patch("musaeus.stages.sentinel.audio_hash_checked")
@patch("musaeus.stages.sentinel.file_hash")
def test_a_new_copy_does_not_double_the_masters_row(mock_fh, mock_ah, ctx, tmp_path):
    master = tmp_path / "Libraries" / "ALAC-Archival" / "Pop" / "A" / "B" / "song.m4a"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"AUDIO")
    upsert_archive(
        ctx.conn,
        {"file_path": str(master), "status": "CATALOGUED", "audio_hash": HASH, "full_hash": "f"},
    )
    # An earlier Act 2 resolved the group and kept the master.
    ctx.conn.execute(
        "INSERT INTO duplicates (group_id, file_path, duplicate_type, status) "
        "VALUES (?, ?, 'EXACT', 'keep')",
        (GROUP, str(master)),
    )
    arrival = tmp_path / "INBOX" / "song.m4a"
    arrival.parent.mkdir()
    arrival.write_bytes(b"AUDIO")
    upsert_archive(ctx.conn, {"file_path": str(arrival), "status": "PENDING"})
    ctx.conn.commit()
    mock_fh.return_value = "f"
    mock_ah.return_value = (HASH, None, "")

    SentinelStage().execute(ctx)

    rows = ctx.conn.execute(
        "SELECT file_path, status FROM duplicates WHERE group_id = ? ORDER BY file_path",
        (GROUP,),
    ).fetchall()
    by_path: dict[str, list[str]] = {}
    for r in rows:
        by_path.setdefault(r["file_path"], []).append(r["status"])
    assert by_path[str(master)] == ["pending"], f"the master's row was doubled: {by_path}"
    assert by_path[str(arrival)] == ["pending"]
