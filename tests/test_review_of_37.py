"""The cloud review of #37 (2026-09-25), one test per finding it made.

Findings 1, 6 and 7 are covered by the updated cases in
test_after_the_225_batch.py. Finding 10 (reuse organize's track-number rule)
is how finding 1 is fixed. Finding 5 turned out deeper than reported: a copy
already set aside could be picked as a duplicate group's KEEPER, and the
library master moved out as its loser.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus import cli
from musaeus.config import MusicConfig
from musaeus.context import RunContext, StageResult
from musaeus.db import open_db, open_hash_index, record_finalized_hash, upsert_archive
from musaeus.stages.base import BaseStage
from musaeus.stages.cross_dupe import CrossDupeStage
from musaeus.stages.dupe_resolver import DupeResolverStage
from musaeus.stages.genre_validate import GenreValidateStage


def _cfg(tmp_path: Path) -> MusicConfig:
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        alac_archive=tmp_path / "Libraries" / "ALAC-Archival",
        db_path=tmp_path / "musaeus.db",
    )
    cfg.ensure_dirs()
    return cfg


def _ctx(tmp_path: Path) -> RunContext:
    cfg = _cfg(tmp_path)
    return RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)


def _row(ctx: RunContext, path: Path, status: str, h: str, *, write=True, **fields) -> None:
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    upsert_archive(ctx.conn, {"file_path": str(path), "status": status, "audio_hash": h, **fields})
    ctx.conn.commit()


# ── 2: a genre borrowed from the lead artist is not an owner ruling ──────────


def test_2_a_later_ruling_for_the_exact_credit_replaces_a_borrowed_genre(tmp_path):
    ctx = _ctx(tmp_path)
    meta = ctx.config.meta_dir
    (meta / "Genre_Allowed.txt").write_text("Rock\nPop\n", encoding="utf-8")
    law = meta / "MasterLaw.csv"
    law.write_text("artist,genre\nPhil Collins,Rock\n", encoding="utf-8")
    _row(ctx, ctx.inbox / "a.m4a", "CATALOGUED", "h", artist="Phil Collins, Marilyn Martin")
    GenreValidateStage().run(ctx)
    law.write_text(
        'artist,genre\nPhil Collins,Rock\n"Phil Collins, Marilyn Martin",Pop\n', encoding="utf-8"
    )
    GenreValidateStage().run(ctx)
    (genre,) = ctx.conn.execute("SELECT genre FROM archive").fetchone()
    assert genre == "Pop", "the borrowed genre was treated as Grey's own ruling"


# ── 3 and 5: rows set aside stay out of duplicate handling ───────────────────


def _master_and_ledger(ctx: RunContext, h: str = "H") -> Path:
    master = ctx.config.alac_archive / "Rock" / "Badfinger" / "B" / "Badfinger - Baby Blue.m4a"
    _row(ctx, master, "CATALOGUED", h, artist="Badfinger", album="B", title="Baby Blue")
    ledger = open_hash_index(ctx.config.hash_index_path)
    record_finalized_hash(ledger, h, str(master))
    ledger.commit()
    ledger.close()
    return master


def test_3_a_deleted_row_is_not_a_cross_batch_candidate(tmp_path):
    ctx = _ctx(tmp_path)
    _master_and_ledger(ctx)
    gone = ctx.inbox / "Badfinger - Baby Blue.m4a"
    _row(ctx, gone, "DELETED", "H", write=False, artist="Badfinger", title="Baby Blue")
    CrossDupeStage().run(ctx)
    assert ctx.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0] == 0
    assert DupeResolverStage().run(ctx).success


@pytest.mark.parametrize("status", ["DUPE_REVIEW", "TRIBUTE_REVIEW", "QUARANTINED"])
def test_5_a_set_aside_copy_never_becomes_the_keeper_of_a_library_master(tmp_path, status):
    ctx = _ctx(tmp_path)
    master = _master_and_ledger(ctx)
    ctx.conn.execute(
        "UPDATE archive SET bitrate = 900000, codec = 'alac' WHERE file_path = ?", (str(master),)
    )
    # Outranks the master on every keeper test: higher bitrate, not a remaster.
    aside = tmp_path / "SET_ASIDE" / "Badfinger - Baby Blue.m4a"
    _row(
        ctx,
        aside,
        status,
        "H",
        artist="Badfinger",
        album="B",
        title="Baby Blue",
        codec="alac",
        bitrate=1_600_000,
    )
    ctx.conn.executemany(
        "INSERT INTO duplicates (group_id, file_path, duplicate_type, status, audio_hash) "
        "VALUES ('dup_x', ?, 'EXACT', 'pending', 'H')",
        [(str(master),), (str(aside),)],
    )
    ctx.conn.commit()
    DupeResolverStage().run(ctx)
    assert master.is_file(), "the library master was moved out in favour of a set-aside copy"
    (fp,) = ctx.conn.execute("SELECT file_path FROM archive WHERE status = 'CATALOGUED'").fetchone()
    assert fp == str(master)
    assert aside.is_file(), "the set-aside copy should be left exactly where it is"


# ── 4, 8, 9: the end-of-run bookkeeping ──────────────────────────────────────


def _stage(
    name, *, changed=0, errored=0, errors=(), fail=False, filed=0, interrupt=False, crash=False
):
    class _Stage(BaseStage):
        NAME = name
        CLAIMS_EFFECT = False

        def validate(self, ctx: RunContext) -> None:
            return None

        def run(self, ctx: RunContext) -> StageResult:
            # Finalize logs one FINALIZE_MOVE per file as it goes; a stage cut
            # short has done that much and recorded nothing else.
            for i in range(filed):
                ctx.log_event("FINALIZE_MOVE", file_path=f"/x/{i}.m4a", stage=name)
            if interrupt:
                raise KeyboardInterrupt
            if crash:
                raise RuntimeError("disk went away")
            result = self._make_result(dry_run=False)
            result.files_changed = changed
            result.files_errored = errored
            result.errors.extend(errors)
            result.success = not fail
            ctx.record_stage(result)
            return result

        def dry_run(self, ctx: RunContext) -> StageResult:
            raise NotImplementedError

    _Stage.__name__ = f"Fake{name.title()}Stage"
    return _Stage


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    # Both patched, always: an unpatched run touches Grey's real resume file.
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(cli, "get_config", lambda: cfg)
    monkeypatch.setattr(cli, "_RESUME_FILE", tmp_path / "resume_state.json")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    published = cfg.libraries / "ALAC_Library_Run_Logs"

    def run(stages):
        return cli._run_pipeline(stages, dry_run=False)

    return run, published


def _published(folder: Path) -> list[Path]:
    return list(folder.glob("run_*")) if folder.exists() else []


def test_4_ctrl_c_during_finalize_itself_still_keeps_the_records(pipeline):
    run, published = pipeline
    assert run([_stage("finalize", filed=150, interrupt=True)]) == 1
    assert len(_published(published)) == 1, "150 tracks were filed and nothing was kept"


def test_4_a_finalize_that_crashes_part_way_still_keeps_the_records(pipeline):
    run, published = pipeline
    run([_stage("finalize", filed=3, crash=True), _stage("audit")])
    assert len(_published(published)) == 1


def test_8_a_stage_with_errored_files_and_no_error_lines_asks_for_attention(pipeline, capsys):
    run, _ = pipeline
    run([_stage("health", errored=2)])
    out = capsys.readouterr()
    text = out.out + out.err
    assert "needs attention" in text
    assert "no problems" not in text, "the act line and the label disagreed"


def test_9_a_second_ctrl_c_while_publishing_still_ends_the_run_cleanly(pipeline, monkeypatch):
    run, _ = pipeline

    def interrupted(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "publish_run_records", interrupted)
    # An escaping KeyboardInterrupt would stop the whole pytest session, not
    # just fail this test -- which is exactly the bug, so catch and report it.
    try:
        code = run([_stage("finalize", changed=5), _stage("forge", interrupt=True)])
    except KeyboardInterrupt:
        pytest.fail("a second Ctrl-C escaped the run's bookkeeping")
    assert code == 1


def test_9_an_interrupted_run_is_held_to_keep_ten_too(pipeline, monkeypatch):
    run, _ = pipeline
    calls = []
    monkeypatch.setattr(cli, "prune_run_records", lambda *a, **k: calls.append(a) or 0)
    run([_stage("finalize", changed=5), _stage("forge", interrupt=True)])
    assert calls, "run records were published but never pruned"
