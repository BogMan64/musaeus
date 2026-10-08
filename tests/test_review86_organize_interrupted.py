"""Review of #86, finding 12 (2026-10-07): Organize renamed files on disk and
committed their rows every 50 files, so a kill could leave up to 49 renamed
masters whose rows still named the old paths. Each rename is now committed as
it completes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.organize import OrganizeStage


@pytest.fixture
def ctx(tmp_path):
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    cfg.inbox.mkdir(parents=True, exist_ok=True)
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def test_a_killed_run_leaves_every_finished_rename_recorded(ctx, monkeypatch):
    for n in range(3):
        path = ctx.inbox / f"flat_{n}.m4a"
        path.write_bytes(b"FAKE AUDIO DATA")
        upsert_archive(ctx.conn, {"file_path": str(path), "status": "CATALOGUED",
                                  "artist": "Test Artist", "album": "Test Album", "title": f"Song {n}"})  # fmt: skip
    ctx.conn.commit()

    real_log, seen = ctx.log_event, []

    def killed_on_the_third(*a, **kw):
        seen.append(kw.get("new_value"))
        if len(seen) == 3:
            raise KeyboardInterrupt  # the run is killed mid-way
        return real_log(*a, **kw)

    monkeypatch.setattr(ctx, "log_event", killed_on_the_third)
    with pytest.raises(KeyboardInterrupt):
        OrganizeStage().run(ctx)

    fresh = sqlite3.connect(ctx.config.db_path)  # what survives the killed process
    on_record = {r[0] for r in fresh.execute("SELECT file_path FROM archive")}
    for renamed in seen[:2]:
        assert Path(renamed).exists()
        assert renamed in on_record, "a finished rename's row rolled back"
