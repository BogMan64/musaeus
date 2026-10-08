"""Review of #86, finding 5 (2026-10-07): NearDupe asked "is this live?" of the
title alone, while the resolver and the keep rule read title AND album. Two
different live recordings whose albums say so -- "Unplugged", "24 Nights (Live)"
-- were grouped, and one was moved to review. NearDupe now uses the resolver's
own rule.
"""

from __future__ import annotations

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.neardupe import NearDupeStage


@pytest.fixture
def ctx(tmp_path):
    meta = tmp_path / "MetaData"
    meta.mkdir()
    (meta / "artist_canon.tsv").write_text("", encoding="utf-8")
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q", runs_root=tmp_path / "RUNS", meta_dir=meta,
        alac_library=tmp_path / "ALAC-Library", db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=True)


def _add(ctx, path, album, duration):
    upsert_archive(ctx.conn, {"file_path": path, "status": "CATALOGUED", "artist": "Eric Clapton",
                              "title": "Layla", "album": album, "duration": duration,
                              "bitrate": 320000, "size_bytes": 5_000_000})  # fmt: skip
    ctx.conn.commit()


def test_two_live_recordings_named_live_by_their_albums_stay_apart(ctx, tmp_path):
    _add(ctx, str(tmp_path / "a.m4a"), "Unplugged", 285.0)
    _add(ctx, str(tmp_path / "b.m4a"), "24 Nights (Live)", 410.0)

    assert NearDupeStage().execute(ctx).files_changed == 0, "two live recordings were grouped"


def test_studio_against_live_is_still_compared(ctx, tmp_path):
    _add(ctx, str(tmp_path / "a.m4a"), "Layla and Other Assorted Love Songs", 423.0)
    _add(ctx, str(tmp_path / "b.m4a"), "Unplugged", 285.0)

    assert NearDupeStage().execute(ctx).files_changed == 1
