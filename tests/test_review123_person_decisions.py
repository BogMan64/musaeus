"""Review of #123 (2026-10-08): gaps in how the resolver carries out a person's
keep ('keep_user') and archive ('archive_user') from `musaeus dedupe`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages.dupe_resolver import DupeResolverStage


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


def _member(ctx, name, group, *, status="pending", album="Album", codec="alac",
            rate=900_000, pcm=None, recorded=None, finalized=None) -> Path:  # fmt: skip
    p = ctx.alac_library / "A" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(name.encode() * 100)
    pcm = pcm or "pcm:" + name
    upsert_archive(ctx.conn, {"file_path": str(p), "status": "CATALOGUED", "artist": "A",
                              "title": "Song", "album": album, "codec": codec, "bitrate": rate,
                              "size_bytes": 1000, "duration": 200.0, "audio_hash": pcm})  # fmt: skip
    if finalized:
        ctx.conn.execute(
            "UPDATE archive SET finalized_at = ? WHERE file_path = ?", (finalized, str(p))
        )
    if group:
        ctx.conn.execute(
            "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, run_id, "
            "audio_hash, status) VALUES (?, ?, 'NEAR', 0.9, 'r', ?, ?)",
            (group, str(p), recorded or pcm, status),
        )
    ctx.conn.commit()
    return p


def _status(ctx, path) -> str:
    return ctx.conn.execute(
        "SELECT status FROM duplicates WHERE file_path = ?", (str(path),)
    ).fetchone()[0]


def _gone(ctx, path: Path) -> None:
    """The file was filed or renamed since: no catalogue row at its old path."""
    path.unlink()
    ctx.conn.execute("DELETE FROM archive WHERE file_path = ?", (str(path),))
    ctx.conn.commit()


def test_a_kept_copy_that_has_gone_does_not_stop_the_group(ctx):
    """Finding 3: the person's kept copy, gone from its path, was made keeper,
    so nothing ever moved and the group came back every run."""
    kept = _member(ctx, "kept.m4a", "near_x", status="keep_user")
    best = _member(ctx, "studio.m4a", "near_x")
    worse = _member(
        ctx, "live.m4a", "near_x", album="Live at the Fillmore", codec="aac", rate=256_000
    )
    _gone(ctx, kept)
    result = DupeResolverStage().execute(ctx)
    assert best.exists() and not worse.exists(), result.errors
    assert any("no longer at" in n for n in result.notes), result.notes


def test_a_stale_group_closes_a_persons_archive_too(ctx):
    """Finding 4: only 'pending' rows were closed as stale, so a group with an
    'archive_user' row came back -- and errored -- on every run."""
    archived = _member(ctx, "a.m4a", "near_x", status="archive_user")
    _member(ctx, "b.m4a", "near_x", recorded="pcm:what-it-was")  # now a different recording
    first = DupeResolverStage().execute(ctx)
    assert archived.exists() and first.errors
    assert _status(ctx, archived) == "stale"
    again = DupeResolverStage().execute(ctx)
    assert not again.errors, again.errors


def test_a_persons_keep_counts_only_for_the_recording_they_kept(ctx):
    """Finding 6a: Source 2 honoured 'keep_user' by path. The path now holds a
    different recording, in a live exact cluster: the keep rule decides it."""
    there = _member(ctx, "p.m4a", "old_group", status="keep_user", pcm="pcm:new",
                    recorded="pcm:what-they-kept")  # fmt: skip
    filed = _member(ctx, "q.m4a", None, pcm="pcm:new", finalized="2026-10-01")
    DupeResolverStage().execute(ctx)
    assert filed.exists(), "the filed copy was moved for a keep made on another recording"
    assert not there.exists()


def test_two_identical_copies_a_person_kept_both_stay_after_one_was_filed(ctx):
    """Finding 6b: one kept copy was filed under a new path; Source 2 matched
    the keeps by path, found one, and moved the other kept copy."""
    one = _member(ctx, "one.m4a", "dup_same", status="keep_user", pcm="pcm:same")
    two = _member(ctx, "two.m4a", "dup_same", status="keep_user", pcm="pcm:same")
    filed = one.with_name("one (filed).m4a")
    one.rename(filed)
    ctx.conn.execute("UPDATE archive SET file_path = ? WHERE file_path = ?", (str(filed), str(one)))
    ctx.conn.commit()
    DupeResolverStage().execute(ctx)
    assert filed.exists() and two.exists()


def test_the_plan_counts_groups_a_person_decided_whole(ctx):
    """Finding 7: only 'pending' was counted; a group decided whole (k on one,
    a on the rest) showed 0 awaiting, and the resolver still moved its files."""
    _member(ctx, "a.m4a", "near_x", status="keep_user")
    _member(ctx, "b.m4a", "near_x", status="archive_user")
    n, _ = DupeResolverStage.plan_candidates(ctx.conn, ctx.config)
    assert n == 1


def test_archiving_every_copy_keeps_one_and_says_so(ctx):
    """Finding 11: a person archived every copy; one is kept so the library is
    never left without the song, and the run now says so."""
    a = _member(ctx, "a.m4a", "near_x", status="archive_user")
    b = _member(ctx, "b.m4a", "near_x", status="archive_user", album="Live at the Fillmore",
                codec="aac", rate=256_000)  # fmt: skip
    result = DupeResolverStage().execute(ctx)
    assert a.exists() != b.exists()
    assert any("every copy" in n for n in result.notes), result.notes
