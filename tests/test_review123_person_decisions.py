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


# ── Review of #129-#134 (2026-10-09): a person's choice names a file, and a file
# is refiled between the choice and the resolver. The choice follows it, by the
# catalogue row it was made on and the recording it held. ──────────────────────


def _console(ctx, monkeypatch, keys: str) -> None:
    import io

    from musaeus import dedupe

    monkeypatch.setattr("sys.stdin", io.StringIO(keys))
    dedupe.run_dedupe_console(ctx.conn)
    ctx.conn.commit()


def _refile(ctx, path: Path, new_name: str) -> Path:
    """As organize and finalize do: the file moves, its row keeps its id."""
    new = path.with_name(new_name)
    path.rename(new)
    ctx.conn.execute("UPDATE archive SET file_path = ? WHERE file_path = ?", (str(new), str(path)))
    ctx.conn.commit()
    return new


def test_a_kept_copy_refiled_since_is_still_the_keeper(ctx, monkeypatch):
    """S-2 (reproduced): keep K, archive Y, then K is filed under a new path.
    The keep rule then kept Y and both decisions were overwritten."""
    studio = _member(ctx, "studio.m4a", "near_x")
    live = _member(ctx, "live.m4a", "near_x", album="Live at the Fillmore", codec="aac",
                   rate=256_000)  # fmt: skip
    _console(ctx, monkeypatch, "2k\n1a\nq\n")  # keep the live copy, archive the studio one
    filed = _refile(ctx, live, "live (filed).m4a")
    DupeResolverStage().execute(ctx)
    assert filed.exists(), "the kept copy was moved"
    assert not studio.exists(), "the archived copy stayed"


def test_two_kept_copies_one_refiled_stay_when_a_third_arrives(ctx, monkeypatch):
    """S-3 (reproduced): keep X and Z, X refiled, an identical W arrives: X' was
    moved with W."""
    x = _member(ctx, "x.m4a", "dup_same", pcm="pcm:same")
    z = _member(ctx, "z.m4a", "dup_same", pcm="pcm:same")
    _console(ctx, monkeypatch, "1k\n2k\nq\n")
    x2 = _refile(ctx, x, "x (filed).m4a")
    w = _member(ctx, "w.m4a", None, pcm="pcm:same")
    DupeResolverStage().execute(ctx)
    assert x2.exists() and z.exists(), "a kept copy was moved"
    assert not w.exists(), "the new identical arrival stayed"


def test_one_file_kept_in_two_groups_does_not_shield_an_identical_arrival(ctx):
    """S-4 (reproduced): the keeps were counted by row, so one file kept in two
    groups looked like two kept copies and an arrival was never moved."""
    f = _member(ctx, "f.m4a", None, pcm="pcm:same")
    for g in ("g1", "g2"):
        ctx.conn.execute(
            "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, run_id, "
            "audio_hash, status) VALUES (?, ?, 'NEAR', 0.9, 'r', 'pcm:same', 'keep_user')",
            (g, str(f)),
        )
    ctx.conn.commit()
    w = _member(ctx, "w.m4a", None, pcm="pcm:same")
    DupeResolverStage().execute(ctx)
    assert f.exists() and not w.exists()


def test_a_set_aside_close_out_does_not_call_a_live_copy_moved(ctx):
    """S-11: the copy left after its partner was set aside was marked 'archive',
    the resolver's word for "already moved" -- never a keeper again."""
    aside = _member(ctx, "aside.m4a", "near_x")
    live = _member(ctx, "live.m4a", "near_x", status="archive_user")
    ctx.conn.execute("UPDATE archive SET status = 'QUARANTINED' WHERE file_path = ?", (str(aside),))
    ctx.conn.commit()
    DupeResolverStage().execute(ctx)
    assert live.exists()
    assert _status(ctx, live) == "stale"


def test_the_console_lists_a_group_decided_but_not_yet_resolved(ctx):
    """S-10: `musaeus status` counted it, the console said "All resolved"."""
    from musaeus import dedupe

    _member(ctx, "a.m4a", "near_x", status="keep_user")
    _member(ctx, "b.m4a", "near_x", status="archive_user")
    assert dedupe._get_pending_groups(ctx.conn) == ["near_x"]
