"""One song held twice under two titles, found by its MusicBrainz work (Grey, 2026-11-01).

"B list items move copies to review": two copies by one act of the same works are
one song; the resolver keeps one by the keep rule. Left alone: a losing copy kept
by hand, a copy under half the other's length, a pair already grouped.
"""

from __future__ import annotations

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages import mb_work as mw
from musaeus.stages.album_fill import Unavailable
from musaeus.stages.dupe_resolver import _get_group_members, _is_live


def _row(i, title, works="w1", artist="The Police", **kw):
    return {"id": i, "file_path": f"/l/{i}.m4a", "audio_hash": f"h{i}", "title": title,
            "album": "Synchronicity", "artist": artist, "codec": "alac", "sample_rate": 44100,
            "duration": 200.0, "mb_work_ids": works, "mb_work_artist": artist, **kw}  # fmt: skip


def test_two_titles_of_one_work_are_one_song_kept_by_the_keep_rule():
    """The live "Synchronicity I (Live In Atlanta ...)" never paired with the studio one."""
    studio = _row(1, "Synchronicity I")
    live = _row(2, "Synchronicity I (Live In Atlanta, 1983 - Remastered 2003)", duration=173.0)

    [(keeper, loser, works)] = mw.same_song_pairs([live, studio], set())

    assert (keeper["id"], loser["id"], works) == (1, 2, "w1")


def test_the_act_is_musicbrainzs_credit_not_our_spelling():
    a = _row(1, "Funky Cold Medina", artist="Tone-Lōc", mb_work_artist="Tone-Lōc")
    b = _row(2, "Funky Cold Medina", artist="Tone-Loc", mb_work_artist="tone-lōc")
    other = _row(3, "Funky Cold Medina", artist="Somebody", mb_work_artist="Somebody")

    assert len(mw.same_song_pairs([a, b, other], set())) == 1


@pytest.mark.parametrize(
    ("b", "why"),
    [
        (_row(2, "Everybody Has A Dream", works="w1,w2"), "holds a second song"),
        (_row(2, "Doolin-Dalton (Instrumental)", duration=48.0), "under half the length"),
        (_row(2, "Synchronicity I", audio_hash="h1"), "the same audio: EXACT finds it"),
        (_row(2, "Synchronicity I (Live)", duration=173.0, kept_by_person_at="2026-10-01"),
         "the losing copy was kept by hand"),
    ],
)  # fmt: skip
def test_left_alone(b, why):
    assert mw.same_song_pairs([_row(1, "Synchronicity I"), b], set()) == [], why


def test_a_pair_already_grouped_is_left_alone():
    a, b = _row(1, "Song"), _row(2, "Song (Single Version)")
    assert mw.same_song_pairs([a, b], {frozenset(("h1", "h2"))}) == []


def test_a_keeper_kept_by_hand_still_sends_the_other_copy():
    keeper = _row(1, "Song", kept_by_person_at="2026-10-01")
    assert len(mw.same_song_pairs([keeper, _row(2, "Song (Live)", duration=180.0)], set())) == 1


def test_musicbrainzs_live_mark_counts_as_live():
    """ "Before The Flood" names no "live"; MusicBrainz marks the recording live."""
    assert _is_live(
        {"title": "Most Likely You Go Your Way", "album": "Before The Flood", "mb_live": 1}
    )
    assert not _is_live({"title": "Most Likely You Go Your Way", "album": "Before The Flood"})


@pytest.fixture
def ctx(tmp_path):
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q", runs_root=tmp_path / "RUNS", meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library", db_path=tmp_path / "musaeus.db",
    )  # fmt: skip
    c = RunContext.new(cfg, open_db(cfg.db_path), dry_run=False)
    from musaeus.stages.acousticid import _ensure_columns

    _ensure_columns(c.conn)
    for i, (title, rec, dur) in enumerate(
        [
            ("Synchronicity I", "rec-studio", 203.0),
            ("Synchronicity I (Live In Atlanta, 1983)", "rec-live", 173.0),
            ("No Recording Known", "", 100.0),
        ]  # fmt: skip
    ):
        fp = f"/l/{i}.m4a"
        upsert_archive(c.conn, {"file_path": fp, "status": "CATALOGUED", "title": title,
                                "artist": "The Police", "codec": "alac", "audio_hash": f"h{i}",
                                "duration": dur, "sample_rate": 44100})  # fmt: skip
        c.conn.execute("UPDATE archive SET acousticid_recording = ? WHERE file_path = ?", (rec, fp))
    c.conn.commit()
    return c


def test_the_stage_asks_stamps_and_stages_a_pending_work_group(ctx, monkeypatch):
    answers = {
        "rec-studio": (["w1"], "The Police", False),
        "rec-live": (["w1"], "The Police", True),
    }
    monkeypatch.setattr(mw, "recording_works", lambda rec: answers[rec])
    monkeypatch.setattr(mw, "_MB_RATE_S", 0)

    mw.MBWorkStage().execute(ctx)

    stamped = ctx.conn.execute(
        "SELECT file_path, mb_work_ids, mb_live FROM archive WHERE mb_work_checked_at IS NOT NULL "
        "ORDER BY file_path"
    ).fetchall()
    assert [tuple(r) for r in stamped] == [("/l/0.m4a", "w1", 0), ("/l/1.m4a", "w1", 1)]
    [gid] = {r[0] for r in ctx.conn.execute(
        "SELECT group_id FROM duplicates WHERE duplicate_type='WORK' AND status='pending'")}  # fmt: skip
    members = _get_group_members(ctx.conn, gid)
    assert [m["file_path"] for m in members] == ["/l/0.m4a", "/l/1.m4a"]  # studio kept
    assert (
        ctx.conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='MB_WORK_DUPE_FOUND' AND file_path='/l/1.m4a'"
        ).fetchone()[0]
        == 1
    )


def test_a_second_run_stages_nothing_new(ctx, monkeypatch):
    monkeypatch.setattr(mw, "recording_works", lambda rec: (["w1"], "The Police", False))
    monkeypatch.setattr(mw, "_MB_RATE_S", 0)
    mw.MBWorkStage().execute(ctx)
    mw.MBWorkStage().execute(ctx)

    assert ctx.conn.execute("SELECT COUNT(DISTINCT group_id) FROM duplicates").fetchone()[0] == 1


def test_an_unreachable_musicbrainz_stamps_nothing(ctx, monkeypatch):
    def down(rec):
        raise Unavailable("503")

    monkeypatch.setattr(mw, "recording_works", down)
    monkeypatch.setattr(mw, "_MB_RATE_S", 0)
    mw.MBWorkStage().execute(ctx)

    assert (
        ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE mb_work_checked_at IS NOT NULL"
        ).fetchone()[0]
        == 0
    )


def _rel(*secondary):
    return {"release-group": {"primary-type": "Album", "secondary-types": list(secondary)}}


@pytest.mark.parametrize(
    ("releases", "live"),
    [
        ([_rel("Live")], True),  # "Before The Flood"
        ([_rel("Live"), _rel("Compilation")], True),  # a hits set leaves it live
        ([_rel("Live", "Compilation")], True),  # a live compilation
        ([_rel("Live"), _rel()], False),  # also on a studio album
        ([_rel("Live"), {"release-group": {"primary-type": "Single"}}], False),  # a studio single
        ([_rel("Compilation")], False),  # nothing says live
        ([_rel("Soundtrack")], False),
        # A live single is live (CodeRabbit, review of #275, declined).
        (
            [
                _rel("Live"),
                {"release-group": {"primary-type": "Single", "secondary-types": ["Live"]}},
            ],
            True,
        ),
        ([], False),
    ],
)
def test_a_recording_on_live_albums_only_is_live(releases, live):
    """Grey, 2026-11-01: MusicBrainz's "Live" album type counts as live ("make it a rule")."""
    assert mw.on_live_albums_only(releases) is live


def test_the_lookup_reads_the_live_albums(monkeypatch):
    data = {"disambiguation": "", "relations": [{"work": {"id": "w1"}}],
            "artist-credit": [{"name": "Bob Dylan"}], "releases": [_rel("Live")]}  # fmt: skip
    monkeypatch.setattr(mw, "_get_json", lambda url: data)

    assert mw.recording_works("rec") == (["w1"], "Bob Dylan", True)


def test_a_recording_on_more_releases_than_listed_is_not_judged_by_its_albums(monkeypatch):
    """CodeRabbit, review of #275: MusicBrainz lists 25 releases; a studio one may be past them."""
    data = {
        "disambiguation": "",
        "relations": [],
        "artist-credit": [],
        "releases": [_rel("Live")] * 25,
    }
    monkeypatch.setattr(mw, "_get_json", lambda url: data)

    assert mw.recording_works("rec")[2] is False
