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
    assert "nothing moved" in out[0]
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


def test_a_kept_copy_refiled_later_is_never_moved_by_the_resolver(ctx, monkeypatch):
    """The keep is on the catalogue row, which keeps its id when refiled: an
    identical arrival later is the one moved."""
    one = _copy(ctx, "one.m4a", "dup_same", pcm="pcm:same")
    two = _copy(ctx, "two.m4a", "dup_same", pcm="pcm:same", finalized="2026-10-01")
    _console(ctx, monkeypatch, "1k\n2k\ny\nq\n")  # the same song on two albums: keep both
    filed = one.with_name("one (filed).m4a")
    one.rename(filed)
    ctx.conn.execute("UPDATE archive SET file_path = ? WHERE file_path = ?", (str(filed), str(one)))
    ctx.conn.commit()
    arrival = _copy(ctx, "arrival.m4a", None, pcm="pcm:same", rate=950_000)
    DupeResolverStage().execute(ctx)
    assert filed.exists() and two.exists(), "a copy the person kept was moved"
    assert not arrival.exists(), "the new identical arrival stayed"


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
