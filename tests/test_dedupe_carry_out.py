"""`musaeus dedupe` carries a choice out at once (Grey, 2026-10-09).

Four reviews running (#86, #123, #129-#134, #135-#139) found a person's keep
and archive misapplied when they were saved for the next Act 2 and files were
refiled in between. Now, when every copy in a set has a choice, the archived
copies move to review there and then -- the resolver's own move, under the
masters lock, re-checked against the catalogue -- and each kept copy is marked
on its catalogue row, which the resolver never moves.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from musaeus import dedupe
from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.masters_lock import masters_lock
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
    cfg.ensure_dirs()
    c = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    c.set("finalize_batch_date", "2026-01-15")
    return c


def _copy(ctx, name, group, *, album="Album", codec="alac", rate=900_000, pcm=None,
          finalized=None) -> Path:  # fmt: skip
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
    for g in [group] if isinstance(group, str) else group or []:
        ctx.conn.execute(
            "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, run_id, "
            "audio_hash) VALUES (?, ?, 'NEAR', 0.9, 'r', ?)", (g, str(p), pcm))  # fmt: skip
    ctx.conn.commit()
    return p


def _pair(ctx) -> tuple[Path, Path]:
    studio = _copy(ctx, "studio.m4a", "near_x")
    live = _copy(ctx, "live.m4a", "near_x", album="Live at the Fillmore", codec="aac", rate=256_000)
    return studio, live  # shown as [1] studio, [2] live


def _console(ctx, monkeypatch, keys: str, carry_out=None) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(keys))
    dedupe.run_dedupe_console(
        ctx.conn,
        carry_out=carry_out if carry_out is not None else dedupe.carry_out_with(ctx.config),
    )


def _statuses(ctx) -> dict[str, str]:
    return {
        Path(p).name + "@" + g: s
        for g, p, s in ctx.conn.execute("SELECT group_id, file_path, status FROM duplicates")
    }


def _kept_mark(ctx, path: Path):
    return ctx.conn.execute(
        "SELECT kept_by_person_at FROM archive WHERE file_path = ?", (str(path),)
    ).fetchone()[0]


def test_a_finished_set_moves_its_archived_copies_at_once(ctx, monkeypatch):
    studio, live = _pair(ctx)
    _console(ctx, monkeypatch, "2k\n1a\ny\nq\n")  # keep the live copy, archive the studio one
    assert live.exists() and not studio.exists(), "not carried out at once"
    assert len(list(ctx.config.dupes_review_dir.rglob("*.m4a"))) == 1, "not in review"
    assert list((ctx.config.dupes_review_dir).rglob("restore_*.sh")), "no restore script"
    assert _kept_mark(ctx, live)
    assert _statuses(ctx) == {"live.m4a@near_x": "keep", "studio.m4a@near_x": "archive"}


def test_archiving_every_copy_is_refused(ctx, monkeypatch):
    studio, live = _pair(ctx)
    _console(ctx, monkeypatch, "1a\n2a\nq\n")
    assert studio.exists() and live.exists()
    assert set(_statuses(ctx).values()) == {"pending"}


@pytest.mark.parametrize("keys", ["2k\nq\n", "2k\n1a\nn\nq\n", "2k\ns\n"])
def test_quitting_skipping_or_saying_no_writes_nothing(ctx, monkeypatch, keys):
    studio, live = _pair(ctx)
    _console(ctx, monkeypatch, keys)
    assert studio.exists() and live.exists()
    assert set(_statuses(ctx).values()) == {"pending"}
    assert not _kept_mark(ctx, live)


def test_a_copy_changed_between_screen_and_confirm_moves_nothing(ctx, monkeypatch):
    studio, live = _pair(ctx)
    real = dedupe.carry_out_with(ctx.config)
    out: list[str] = []

    def changed_then_carry_out(groups, kept, archived):
        # Something refiled another recording onto the studio path meanwhile.
        ctx.conn.execute("UPDATE archive SET audio_hash = 'pcm:other' WHERE file_path = ?",
                         (str(studio),))  # fmt: skip
        ctx.conn.commit()
        out.append(real(groups, kept, archived))
        return out[-1]

    _console(ctx, monkeypatch, "2k\n1a\ny\nq\n", carry_out=changed_then_carry_out)
    assert studio.exists() and live.exists()
    assert out[0][0] is False and "nothing moved" in out[0][1]
    assert set(_statuses(ctx).values()) == {"pending"}


def test_a_busy_masters_lock_moves_and_writes_nothing(ctx, monkeypatch, capsys):
    studio, live = _pair(ctx)
    with masters_lock(ctx.config.runs_root, exclusive=True, what="a bit-rot check"):
        _console(ctx, monkeypatch, "2k\n1a\ny\nq\n")
    assert studio.exists() and live.exists()
    assert set(_statuses(ctx).values()) == {"pending"}
    assert "in use" in capsys.readouterr().out


def test_a_file_in_two_groups_is_shown_once_and_kept_in_both(ctx, monkeypatch):
    """Review of #135-#139, finding 1: kept in one group, then moved through the
    other. The groups that share it are one set."""
    k = _copy(ctx, "k.m4a", ["near_a", "near_b"])
    b = _copy(ctx, "b.m4a", "near_a", album="Live at the Fillmore", codec="aac", rate=256_000)
    c = _copy(ctx, "c.m4a", "near_b", finalized="2026-10-01")
    sets = dedupe._get_sets(ctx.conn)
    assert sets == [["near_a", "near_b"]]
    order = [Path(m["file_path"]).name for m in dedupe._set_members(ctx.conn, sets[0])]
    assert sorted(order) == ["b.m4a", "c.m4a", "k.m4a"]
    keys = "".join(f"{order.index(n) + 1}{a}\n" for n, a in (("k.m4a", "k"), ("b.m4a", "a"),
                                                            ("c.m4a", "a"))) + "y\nq\n"  # fmt: skip
    _console(ctx, monkeypatch, keys)
    assert k.exists() and not b.exists() and not c.exists()
    s = _statuses(ctx)
    assert s["k.m4a@near_a"] == s["k.m4a@near_b"] == "keep"


def test_the_numbers_stay_on_the_files_shown(ctx, monkeypatch):
    """Review of #129-#134, finding 1: "1a" then "3k" kept the copy just
    archived, as the group had been re-ranked and not shown again."""
    a = _copy(ctx, "a.m4a", "near_x", rate=900_000)
    b = _copy(ctx, "b.m4a", "near_x", rate=800_000)
    c = _copy(ctx, "c.m4a", "near_x", rate=700_000)
    shown = [Path(m["file_path"]).name for m in dedupe._set_members(ctx.conn, ["near_x"])]
    assert shown == ["a.m4a", "b.m4a", "c.m4a"]
    _console(ctx, monkeypatch, "1a\n3k\n2a\ny\nq\n")
    assert c.exists() and not a.exists() and not b.exists()


def test_the_resolver_leaves_a_carried_out_set_alone(ctx, monkeypatch):
    studio, live = _pair(ctx)
    _console(ctx, monkeypatch, "2k\n1a\ny\nq\n")
    result = DupeResolverStage().execute(ctx)
    assert live.exists() and not result.errors, result.errors


def test_a_kept_pair_is_left_alone_until_another_copy_arrives(ctx, monkeypatch):
    """The same song on two albums, both kept: left alone, also once one is
    refiled (the mark is on the catalogue row). A copy that arrives later is
    ranked with them by the keep rule (Grey, 2026-10-10)."""
    one = _copy(ctx, "one.m4a", "dup_same", pcm="pcm:same")
    two = _copy(ctx, "two.m4a", "dup_same", pcm="pcm:same", finalized="2026-10-01")
    _console(ctx, monkeypatch, "1k\n2k\ny\nq\n")
    filed = one.with_name("one (filed).m4a")
    one.rename(filed)
    ctx.conn.execute("UPDATE archive SET file_path = ? WHERE file_path = ?", (str(filed), str(one)))
    ctx.conn.commit()
    DupeResolverStage().execute(ctx)
    assert filed.exists() and two.exists(), "a pair the person kept was split"
    arrival = _copy(ctx, "arrival.m4a", None, pcm="pcm:same", rate=950_000)
    DupeResolverStage().execute(ctx)
    assert sum(p.exists() for p in (filed, two, arrival)) == 1, "not ranked by the keep rule"


def test_a_set_aside_close_out_does_not_call_a_live_copy_moved(ctx):
    """Review of #129-#134, finding 11: the copy left after its partner was set
    aside was marked 'archive' ("already moved"), never a keeper again."""
    aside = _copy(ctx, "aside.m4a", "near_x")
    live = _copy(ctx, "live.m4a", "near_x")
    ctx.conn.execute("UPDATE archive SET status = 'QUARANTINED' WHERE file_path = ?", (str(aside),))
    ctx.conn.commit()
    DupeResolverStage().execute(ctx)
    assert live.exists()
    assert _statuses(ctx)["live.m4a@near_x"] == "stale"


# ── Review of #140-#142 (2026-10-10) ──


def test_a_stop_during_a_carry_out_waits_until_it_is_done(ctx, monkeypatch):
    """Finding 1: Ctrl-C between moves left the keep unrecorded."""
    import os
    import signal

    studio, live = _pair(ctx)
    real = DupeResolverStage._move_losers

    def interrupted(self, *args, **kwargs):
        os.kill(os.getpid(), signal.SIGINT)  # Ctrl-C while moving
        return real(self, *args, **kwargs)

    monkeypatch.setattr(DupeResolverStage, "_move_losers", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _console(ctx, monkeypatch, "2k\n1a\ny\nq\n")
    assert live.exists() and not studio.exists(), "the carry-out did not finish"
    assert _kept_mark(ctx, live)
    assert _statuses(ctx) == {"live.m4a@near_x": "keep", "studio.m4a@near_x": "archive"}


def test_a_later_better_copy_is_ranked_with_the_kept_one(ctx, monkeypatch):
    """Grey, 2026-10-10: a keep settles the copies the person saw; a copy that
    arrives later is ranked with it by the keep rule (finding 2)."""
    studio, live = _pair(ctx)
    _console(ctx, monkeypatch, "2k\n1a\ny\nq\n")  # keep the live AAC copy
    better = _copy(ctx, "studio96.m4a", "near_new")
    ctx.conn.execute("INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, "
                     "run_id, audio_hash) VALUES ('near_new', ?, 'NEAR', 0.9, 'r', 'pcm:live.m4a')",
                     (str(live),))  # fmt: skip
    ctx.conn.commit()
    DupeResolverStage().execute(ctx)
    assert better.exists() and not live.exists()


def test_copies_all_kept_by_a_person_are_left_alone_and_closed(ctx, monkeypatch):
    """Finding 8: a NEAR pair of two kept copies stayed pending for ever."""
    a = _copy(ctx, "a.m4a", "near_x")
    b = _copy(ctx, "b.m4a", "near_x", album="Live at the Fillmore", codec="aac", rate=256_000)
    _console(ctx, monkeypatch, "1k\n2k\ny\nq\n")
    ctx.conn.execute("UPDATE duplicates SET status = 'pending'")  # flagged again later
    ctx.conn.commit()
    DupeResolverStage().execute(ctx)
    assert a.exists() and b.exists()
    assert set(_statuses(ctx).values()) == {"keep"}


def test_a_file_kept_in_a_closed_row_joins_the_sets_it_is_in(ctx):
    """Finding 3: a file 'keep' in one group and pending in another was shown in
    two sets."""
    p = _copy(ctx, "p.m4a", ["dup_p", "near_b"])
    _copy(ctx, "n.m4a", "dup_p")
    _copy(ctx, "r.m4a", "near_b")
    ctx.conn.execute("UPDATE duplicates SET status = 'keep' WHERE group_id = 'dup_p' AND file_path = ?",
                     (str(p),))  # fmt: skip
    ctx.conn.commit()
    assert dedupe._get_sets(ctx.conn) == [["dup_p", "near_b"]]


def test_two_carry_outs_keep_two_restore_scripts_and_finish_their_runs(ctx, monkeypatch):
    """Findings 6 and 9: one-second names overwrote a restore script; a carry-out
    left a run with no RUN_END."""
    _pair(ctx)
    _copy(ctx, "x.m4a", "near_y")
    _copy(ctx, "y.m4a", "near_y", album="Live at the Fillmore", codec="aac", rate=256_000)
    _console(ctx, monkeypatch, "2k\n1a\ny\n2k\n1a\ny\nq\n")
    assert len(list(ctx.config.dupes_review_dir.rglob("restore_*.sh"))) == 2
    starts, ends = (
        ctx.conn.execute("SELECT COUNT(*) FROM events WHERE event_type = ?", (e,)).fetchone()[0]
        for e in ("RUN_START", "RUN_END")
    )
    assert starts - ends == 1, "a carry-out's run was left open"  # this test's own ctx stays open


def test_a_refused_carry_out_is_not_counted(ctx, monkeypatch, capsys):
    """Finding 10."""
    _pair(ctx)
    with masters_lock(ctx.config.runs_root, exclusive=True, what="a backup"):
        _console(ctx, monkeypatch, "2k\n1a\ny\ns\n")
    assert "Carried out=0" in capsys.readouterr().out
