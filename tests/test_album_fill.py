"""AlbumFillStage: an empty album from AcoustID, then Discogs, then Deezer.

Grey, 2026-10-03: the three album scripts of 2026-09 wired into Enrichment,
their rules unchanged -- album type only, our artist, earliest release,
anything ambiguous LEFT EMPTY, never overwrite, a version title never
searched by text. The network is stood in for; the rules are what is tested.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages import album_fill as af
from musaeus.stages.acousticid import _ensure_columns as fingerprint_columns
from musaeus.stages.album_fill import AlbumFillStage, Unavailable


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q", runs_root=tmp_path / "RUNS", meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC_Library", db_path=tmp_path / "musaeus.db",
        acousticid_api_key="k", discogs_consumer_key="dk", discogs_consumer_secret="ds",
    )  # fmt: skip
    cfg.ensure_dirs()
    conn = open_db(cfg.db_path)
    fingerprint_columns(conn)
    monkeypatch.setattr(af.time, "sleep", lambda *_: None)
    calls: dict[str, list] = {"acoustid": [], "mb": [], "discogs": [], "deezer": []}
    answers: dict[str, object] = {"acoustid": [], "mb": {}, "discogs": [], "deezer": []}

    def _aid(fp, dur, key):
        calls["acoustid"].append(fp)
        a = answers["acoustid"]
        if isinstance(a, Exception):
            raise a
        return a(fp) if callable(a) else a

    monkeypatch.setattr(af, "acoustid_release_groups", _aid)
    monkeypatch.setattr(
        af, "mb_first_release", lambda rg: calls["mb"].append(rg) or answers["mb"].get(rg)
    )
    monkeypatch.setattr(
        af, "discogs_search", lambda a, t, k, s: calls["discogs"].append(t) or answers["discogs"]
    )
    monkeypatch.setattr(
        af, "deezer_search", lambda a, t: calls["deezer"].append(t) or answers["deezer"]
    )

    def add(title, artist="Toto", album=None, fp="FP"):
        path = str(cfg.vault_root / f"{artist} - {title}.m4a")
        upsert_archive(conn, {"file_path": path, "filename": Path(path).name, "ext": ".m4a",
                              "status": "CATALOGUED", "artist": artist, "title": title, "album": album})  # fmt: skip
        conn.execute("UPDATE archive SET chromaprint=?, chromaprint_duration=? WHERE file_path=?",
                     (fp, 200.0 if fp else None, path))  # fmt: skip
        conn.commit()
        return path

    def run(dry_run=False):
        ctx = RunContext.new(cfg, conn, dry_run=dry_run)
        stage = AlbumFillStage()
        return (stage.dry_run(ctx) if dry_run else stage.run(ctx)), ctx

    def album(path):
        row = conn.execute("SELECT * FROM archive WHERE file_path=?", (path,)).fetchone()
        # sqlite3.Row: `in row` tests VALUES, so the column test needs .keys().
        has_stamp = "album_fill_checked_at" in row.keys()  # noqa: SIM118
        return (row["album"], row["album_fill_checked_at"] if has_stamp else None)

    names = {"cfg": cfg, "conn": conn, "add": add, "run": run, "album": album}
    return type("E", (), {**names, "calls": calls, "answers": answers})


def _rg(rid, title, artist="Toto", type_="Album", secondary=None):
    return {"id": rid, "title": title, "type": type_, "secondarytypes": secondary or [],
            "artists": [{"name": artist}]}  # fmt: skip


def test_a_single_clean_album_from_the_fingerprint(env):
    p = env.add("Rosanna")
    env.answers["acoustid"] = [_rg("r1", "Toto IV"), _rg("r2", "Hits", secondary=["Compilation"])]
    env.run()
    assert env.album(p)[0] == "Toto IV" and env.album(p)[1]
    assert env.calls["discogs"] == [] and env.calls["deezer"] == [], (
        "later sites only for leftovers"
    )


def test_the_earliest_release_wins_and_each_album_is_dated_once(env):
    p1, p2 = env.add("Bargain", artist="The Who"), env.add("Going Mobile", artist="The Who")
    env.answers["acoustid"] = [
        _rg("who", "Who's Next", "The Who"),
        _rg("fd", "Face Dances", "The Who"),
    ]
    env.answers["mb"] = {"who": "1971-08-14", "fd": "1981-03-16"}
    env.run()
    assert env.album(p1)[0] == env.album(p2)[0] == "Who's Next"
    assert sorted(env.calls["mb"]) == ["fd", "who"], "one MusicBrainz lookup per album, cached"


def test_an_album_crediting_someone_else_is_left_empty(env):
    p = env.add("Bargain", artist="The Who")
    env.answers["acoustid"] = [
        _rg("a", "Gospel One", "God's Property"),
        _rg("b", "Gospel Two", "Kirk Franklin"),
    ]
    env.answers["deezer"] = []
    env.run()
    assert env.album(p)[0] is None and env.album(p)[1], "answered, so stamped, and left empty"


def test_discogs_then_deezer_only_for_what_the_fingerprint_left(env):
    p = env.add("Rockmaker")
    env.answers["acoustid"] = []
    env.answers["discogs"] = [
        {"title": "Toto - Rockmaker", "format": ["Vinyl", "Single"], "year": "1978"},
        {"title": "Toto - Toto", "format": ["Vinyl", "Album"], "year": "1978"},
    ]
    env.run()
    assert env.album(p)[0] == "Toto"
    assert env.calls["deezer"] == []


def test_deezer_last_exact_match_studio_album_only(env):
    p = env.add("Africa")
    env.answers["acoustid"] = []
    env.answers["discogs"] = []
    env.answers["deezer"] = [
        {"artist": {"name": "Toto"}, "title": "Africa", "album": {"title": "Greatest Hits"}},
        {"artist": {"name": "Toto"}, "title": "Africa", "album": {"title": "Toto IV"}},
    ]
    env.run()
    assert env.album(p)[0] == "Toto IV"


def test_a_version_title_is_never_searched_by_text(env):
    p = env.add("Rosanna (Live)")
    env.answers["acoustid"] = [_rg("x", "Live Hits", secondary=["Live"])]
    env.run()
    assert env.calls["discogs"] == [] and env.calls["deezer"] == []
    assert env.album(p)[0] is None


def test_an_existing_album_is_never_touched(env):
    p = env.add("Rosanna", album="Toto IV")
    env.run()
    assert env.calls["acoustid"] == [] and env.album(p)[0] == "Toto IV"


def test_no_answer_is_not_an_answer_ask_again_next_run(env):
    p = env.add("Rosanna")
    env.answers["acoustid"] = Unavailable("timeout")
    env.run()
    assert env.album(p) == (None, None), "unstamped"
    env.answers["acoustid"] = [_rg("r1", "Toto IV")]
    env.run()
    assert env.album(p)[0] == "Toto IV"


def test_dry_run_asks_no_site_and_writes_nothing(env):
    p = env.add("Rosanna")
    result, _ = env.run(dry_run=True)
    assert all(v == [] for v in env.calls.values())
    assert env.album(p)[0] is None and result.files_processed == 1
    cols = [r[1] for r in env.conn.execute("PRAGMA table_info(archive)")]
    assert "album_fill_checked_at" not in cols, "a dry run does not even add its column"


def test_filled_rows_are_recorded_as_events(env):
    p = env.add("Rosanna")
    env.answers["acoustid"] = [_rg("r1", "Toto IV")]
    _, ctx = env.run()
    ev = env.conn.execute(
        "SELECT new_value, note FROM events WHERE event_type='ALBUM_FILLED' AND file_path=?", (p,)
    ).fetchone()
    assert ev[0] == "Toto IV" and ev[1].startswith("acoustid:")


def test_a_playlist_name_is_not_an_album_and_is_replaced(env):
    """Grey, 2026-10-03: "My playlist S" (1,841 songs) counts as no album."""
    p = env.add("Rosanna", album="My playlist R")
    env.answers["acoustid"] = [_rg("r1", "Toto IV")]
    env.run()
    assert env.album(p)[0] == "Toto IV"


def test_a_playlist_name_left_when_nothing_is_certain(env):
    p = env.add("Rosanna", album="My playlist R")
    env.answers["acoustid"] = [_rg("a", "One"), _rg("b", "Two")]
    env.answers["mb"] = {"a": "1980", "b": "1980"}
    env.answers["deezer"] = []
    env.run()
    assert env.album(p)[0] == "My playlist R", "ambiguous: nothing written"
