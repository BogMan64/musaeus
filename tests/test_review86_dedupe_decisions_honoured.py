"""Review of #86, finding 9 (2026-10-07), the part Grey asked for (2026-10-08:
"yes", he uses `musaeus dedupe`): the console wrote 'keep'/'archive', words the
resolver reads as its own bookkeeping -- 'archive' meant "already moved", so an
archived copy was never moved, and a group a person decided whole had nothing
pending and was skipped. A person's decision is now 'keep_user'/'archive_user',
and the resolver carries it out: the kept copy is the keeper, the archived ones
move.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from musaeus import dedupe
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.dupe_resolver import DupeResolverStage
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
    c = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    c.set("finalize_batch_date", "2026-01-15")
    return c


def _pair(ctx) -> tuple[Path, Path]:
    paths = []
    for name, album, codec, rate in (("studio.m4a", "Album", "alac", 900_000),
                                     ("live.m4a", "Live at the Fillmore", "aac", 256_000)):  # fmt: skip
        p = ctx.alac_library / "A" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(name.encode() * 100)
        upsert_archive(ctx.conn, {"file_path": str(p), "status": "CATALOGUED", "artist": "A",
                                  "title": "Song", "album": album, "codec": codec,
                                  "bitrate": rate, "size_bytes": 1000, "duration": 200.0,
                                  "audio_hash": "pcm:" + name})  # fmt: skip
        ctx.conn.execute(
            "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, run_id, "
            "audio_hash) VALUES ('near_x', ?, 'NEAR', 0.9, 'r', ?)", (str(p), "pcm:" + name))  # fmt: skip
        paths.append(p)
    ctx.conn.commit()
    return paths[0], paths[1]


def _console(ctx, monkeypatch, keys: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(keys))
    dedupe.run_dedupe_console(ctx.conn)
    ctx.conn.commit()


def test_a_persons_keep_and_archive_are_carried_out(ctx, monkeypatch):
    studio, live = _pair(ctx)  # member 1 is the studio ALAC, member 2 the live AAC
    _console(ctx, monkeypatch, "2k\n1a\nq\n")  # Grey keeps the live copy

    DupeResolverStage().execute(ctx)

    assert live.exists(), "the copy the person kept was moved"
    assert not studio.exists(), "the copy the person archived was never moved"


def test_keeping_both_still_keeps_both(ctx, monkeypatch):
    studio, live = _pair(ctx)
    _console(ctx, monkeypatch, "1k\n2k\nq\n")

    DupeResolverStage().execute(ctx)
    staged = NearDupeStage().execute(ctx).files_changed

    assert studio.exists() and live.exists()
    assert staged == 0, "a pair the person kept both of was found again"
