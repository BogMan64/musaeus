"""AlbumFillStage: an empty album from AcoustID, then Discogs, then Deezer.

Grey, 2026-10-03: the three album scripts of 2026-09 wired into Enrichment,
their rules unchanged -- album type only, our artist, earliest release,
anything ambiguous LEFT EMPTY, never overwrite, a version title never
searched by text. The network is stood in for; the rules are what is tested.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import open_db, upsert_archive
from musaeus.stages import album_fill as af
from musaeus.stages.acousticid import _ensure_columns as fingerprint_columns
from musaeus.stages.album_fill import AlbumFillStage, Unavailable

# The fixture stands in for it; a test that checks the real one puts it back.
_REAL_ACOUSTID_RELEASE_GROUPS = af.acoustid_release_groups


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
    calls: dict[str, list] = {
        "acoustid": [], "mb": [], "discogs": [], "deezer": [], "itunes": [], "confirm": []
    }  # fmt: skip
    answers: dict[str, object] = {
        "acoustid": [],
        "mb": {},
        "discogs": [],
        "deezer": [],
        "itunes": [],
        # MusicBrainz's yes or no to a text source's album: a set of album names, or True
        "confirm": True,
        # ... and the albums it confirms only when a live album may count
        "confirm_live": set(),
    }

    def _aid(fp, dur, key, artist, title):
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
    monkeypatch.setattr(
        af,
        "itunes_search",
        lambda a, t: (
            calls["itunes"].append(t)
            or (answers["itunes"](t) if callable(answers["itunes"]) else answers["itunes"])
        ),
    )

    def _confirm(artist, album, title, live_ok=False):
        calls["confirm"].append(album)
        yes = answers["confirm"]
        live = answers["confirm_live"]
        return (yes is True or album in yes or (live_ok and album in live)), "mb"  # type: ignore[operator]

    monkeypatch.setattr(af, "mb_confirm", _confirm)

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


def _acoustid_reply(meta):
    """AcoustID as it answers: one fingerprint linked to two recordings, one of
    them wrong. Asked for recordings, the albums come per recording; asked for
    release groups alone, every linked recording's albums come as one list."""
    recs = [
        {"id": "soul", "title": "All About Soul", "artists": [{"name": "Billy Joel"}],
         "releasegroups": [_rg("rod", "River of Dreams", "Billy Joel")]},
        {"id": "falls", "title": "Summer, Highland Falls", "artists": [{"name": "Billy Joel"}],
         "releasegroups": [_rg("turn", "Turnstiles", "Billy Joel")]},
    ]  # fmt: skip
    if "recordings" in meta.split():
        return {"status": "ok", "results": [{"score": 0.98, "recordings": recs}]}
    groups = [g for r in recs for g in r["releasegroups"]]
    return {"status": "ok", "results": [{"score": 0.98, "releasegroups": groups}]}


def test_only_the_albums_of_the_recording_that_matches_the_song(env, monkeypatch):
    # Grey, 2026-10-22: "All About Soul" was filed under Turnstiles, an album
    # of a recording the fingerprint is wrongly linked to, because the
    # earliest album of every linked recording won.
    monkeypatch.setattr(af, "acoustid_release_groups", _REAL_ACOUSTID_RELEASE_GROUPS)
    monkeypatch.setattr(
        af, "_get_json", lambda url: _acoustid_reply(parse_qs(urlparse(url).query)["meta"][0])
    )
    p = env.add("All About Soul", artist="Billy Joel")
    env.answers["mb"] = {"rod": "1993-08-10", "turn": "1976-05-19"}
    env.run()
    assert env.album(p)[0] == "River Of Dreams"  # stored as Normalize stores it (2026-10-11)


def test_no_recording_matching_the_song_gives_no_album_from_the_fingerprint(env, monkeypatch):
    monkeypatch.setattr(af, "acoustid_release_groups", _REAL_ACOUSTID_RELEASE_GROUPS)
    monkeypatch.setattr(
        af, "_get_json", lambda url: _acoustid_reply(parse_qs(urlparse(url).query)["meta"][0])
    )
    p = env.add("Piano Man", artist="Billy Joel")
    env.answers["mb"] = {"rod": "1993-08-10", "turn": "1976-05-19"}
    env.run()
    assert env.album(p)[0] is None and env.calls["deezer"] == ["Piano Man"], (
        "the text sites are asked instead"
    )


def _aid_rec(rid, title, artist, *groups):
    return {"id": rid, "title": title, "artists": [{"name": artist}] if artist else [],
            "releasegroups": list(groups)}  # fmt: skip


def test_the_song_is_found_in_a_later_result_as_the_acoustid_stage_finds_it(monkeypatch):
    # Codex review of #223: only the top result was read.
    monkeypatch.setattr(af, "_get_json", lambda url: {"status": "ok", "results": [
        {"score": 0.99, "recordings": [_aid_rec("x", "Piano Man", "Billy Joel", _rg("pm", "Piano Man"))]},
        {"score": 0.95, "recordings": [_aid_rec("s", "All About Soul", "Billy Joel",
                                            _rg("rod", "River of Dreams", "Billy Joel"))]},
    ]})  # fmt: skip
    groups = af.acoustid_release_groups("FP", 200.0, "k", "Billy Joel", "All About Soul")
    assert [g["title"] for g in groups] == ["River of Dreams"]


def test_a_recording_with_no_artist_credit_is_not_the_song(monkeypatch):
    # Codex review of #223: "Hurt" with no artist took another act's album.
    monkeypatch.setattr(af, "_get_json", lambda url: {"status": "ok", "results": [
        {"score": 0.99, "recordings": [_aid_rec("h", "Hurt", "", _rg("d", "The Downward Spiral", "Nine Inch Nails"))]},
    ]})  # fmt: skip
    assert af.acoustid_release_groups("FP", 200.0, "k", "Johnny Cash", "Hurt") == []


def test_a_song_with_no_artist_is_not_asked_and_not_stamped(env, monkeypatch):
    # Codex review of #223, round 2: with no artist to check, any "Hurt" agreed.
    monkeypatch.setattr(af, "acoustid_release_groups", _REAL_ACOUSTID_RELEASE_GROUPS)
    monkeypatch.setattr(af, "_get_json", lambda url: {"status": "ok", "results": [
        {"score": 0.99, "recordings": [_aid_rec("h", "Hurt", "Nine Inch Nails",
                                                _rg("d", "The Downward Spiral", "Nine Inch Nails"))]},
    ]})  # fmt: skip
    p = env.add("Hurt", artist="")
    env.run()
    assert env.album(p) == (None, None), "nothing to check it against: asked again later"


def test_an_album_with_no_credit_of_its_own_is_not_ours(monkeypatch):
    # Codex review of #223, rounds 3 and 4: it could be a various-artists
    # album. Unknown is left empty; a wrong album is worse than none.
    rod = {**_rg("rod", "River of Dreams"), "artists": []}
    monkeypatch.setattr(af, "_get_json", lambda url: {"status": "ok", "results": [
        {"score": 0.99, "recordings": [_aid_rec("s", "All About Soul", "Billy Joel", rod)]},
    ]})  # fmt: skip
    groups = af.acoustid_release_groups("FP", 200.0, "k", "Billy Joel", "All About Soul")
    assert af.choose_from_release_groups(groups, "Billy Joel", lambda _: None)[0] is None


def test_a_matching_recording_with_no_albums_does_not_hide_one_with_albums(monkeypatch):
    # Codex review of #223, round 4.
    monkeypatch.setattr(af, "_get_json", lambda url: {"status": "ok", "results": [
        {"score": 0.99, "recordings": [
            _aid_rec("bare", "All About Soul", "Billy Joel"),
            _aid_rec("s", "All About Soul", "Billy Joel", _rg("rod", "River of Dreams", "Billy Joel")),
        ]},
    ]})  # fmt: skip
    groups = af.acoustid_release_groups("FP", 200.0, "k", "Billy Joel", "All About Soul")
    assert [g["title"] for g in groups] == ["River of Dreams"]


def test_a_song_with_no_title_is_not_asked_and_not_stamped(env):
    # Codex review of #223, round 3: no title, no recording can agree.
    p = env.add("", artist="Billy Joel")
    env.answers["acoustid"] = [_rg("rod", "River of Dreams", "Billy Joel")]
    env.run()
    assert env.album(p) == (None, None) and env.calls["acoustid"] == []


def test_one_clean_album_crediting_someone_else_is_not_ours():
    groups = [_rg("d", "The Downward Spiral", "Nine Inch Nails")]
    assert af.choose_from_release_groups(groups, "Johnny Cash", lambda _: None)[0] is None


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


def test_a_discogs_or_deezer_album_is_taken_only_if_musicbrainz_confirms(env):
    """Grey, 2026-10-22: Discogs and Deezer filled the Odds & Sods songs with
    "The Golden Cream Of The Country" and a film soundtrack. Unconfirmed, the
    next source is asked."""
    p = env.add("Africa")
    env.answers["acoustid"] = []
    env.answers["discogs"] = [
        {"title": "Toto - Golden Cream", "format": ["LP", "Album"], "year": "1969"}
    ]
    env.answers["deezer"] = [
        {"artist": {"name": "Toto"}, "title": "Africa", "album": {"title": "Toto IV"}}
    ]
    env.answers["confirm"] = {"Toto IV"}
    env.run()
    assert env.calls["confirm"] == ["Golden Cream", "Toto IV"]
    assert env.album(p)[0] == "Toto IV"


def test_an_unconfirmed_text_album_is_left_empty_and_stamped(env):
    p = env.add("Hold The Line")
    env.answers["acoustid"] = []
    env.answers["deezer"] = [
        {"artist": {"name": "Toto"}, "title": "Hold The Line", "album": {"title": "Mermaids"}}
    ]
    env.answers["confirm"] = set()
    env.run()
    assert env.calls["confirm"] == ["Mermaids"]
    assert env.album(p)[0] is None and env.album(p)[1], "answered, so stamped"
    note = env.conn.execute(
        "SELECT note FROM events WHERE event_type='ALBUM_LEFT_EMPTY'"
    ).fetchone()[0]
    assert note.startswith("itunes:"), "every source was asked"


def test_musicbrainz_is_asked_for_official_albums_only(monkeypatch):
    """Unfiltered, the first answers for "Brilliant Disguise" on "Tunnel Of Love"
    were all bootlegs, and the studio album was never confirmed (2026-10-22)."""
    urls = []
    monkeypatch.setattr(af.time, "sleep", lambda *_: None)
    monkeypatch.setattr(af, "_get_json", lambda url, headers=None: urls.append(url) or {})
    af.mb_confirm("Bruce Springsteen", "Tunnel Of Love", "Brilliant Disguise")
    query = parse_qs(urlparse(urls[0]).query)["query"][0]
    assert "status:official" in query and "primarytype:album" in query


def test_a_version_title_is_never_searched_by_text(env):
    p = env.add("Rosanna (Live)")
    env.answers["acoustid"] = [_rg("x", "Live Hits", secondary=["Live"])]
    env.run()
    assert env.calls["discogs"] == [] and env.calls["deezer"] == []
    assert env.album(p)[0] is None


def test_an_existing_album_is_never_touched(env):
    p = env.add("Rosanna", album="Toto IV")
    env.run()
    # Its fingerprint may be asked once, to check the arriving album (2026-10-23).
    assert len(env.calls["acoustid"]) <= 1 and env.album(p)[0] == "Toto IV"


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


def test_a_filled_album_is_stored_with_every_word_capitalised(env):
    """Grey, 2026-10-11: albums take every word capitalised, in tags and folders.
    album-fill wrote MusicBrainz's "Exile on Main St." as it came; Act 3 filed the
    song under "Exile on Main St", and Normalize moved it to "Exile On Main St"
    on the next run (batch 3, 2026-10-25). Grey: "Exile On Main St" is the standard."""
    p = env.add("Tumbling Dice")
    env.answers["acoustid"] = [_rg("r1", "Exile on Main St.")]
    env.run()
    assert env.album(p)[0] == "Exile On Main St."
    ev = env.conn.execute("SELECT new_value FROM events WHERE event_type='ALBUM_FILLED'").fetchone()
    assert ev[0] == "Exile On Main St."


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


def _it(track, album, date, artist="Toto", count=10):
    return {"artistName": artist, "trackName": track, "collectionName": album, "releaseDate": date + "T08:00:00Z", "trackCount": count}  # fmt: skip


def test_itunes_earliest_exact_studio_album_wins():
    items = [
        _it("Rosanna", "Toto IV", "1982-03-01"),
        _it("ROSANNA", "Guilty Pleasures", "1982-04-01", count=17),
        _it("Rosanna (Live)", "Absolutely Live", "1993-10-12", count=8),
        _it("Rosanna (Single Version)", "Essential - 80s Love", "1982-03-31", count=20),
    ]
    assert af.choose_from_itunes(items, "Toto", "Rosanna") == (
        "Toto IV",
        "earliest of 2 (1982-03-01)",
    )


@pytest.mark.parametrize(
    ("album", "count"),
    [("Rosanna - Single", 2), ("Rosanna - EP", 4), ("Toto IV (Deluxe Edition)", 20), ("Toto IV (Remastered)", 10),
     ("Greatest Hits", 18), ("Toto IV", 3)],
)  # fmt: skip
def test_itunes_skips_singles_eps_reissues_compilations(album, count):
    assert (
        af.choose_from_itunes(
            [_it("Rosanna", album, "1982-03-01", count=count)], "Toto", "Rosanna"
        )[0]
        is None
    )


def test_itunes_two_albums_on_one_date_is_a_tie_and_left_empty():
    items = [_it("Africa", "Album One", "1982-01-01"), _it("Africa", "Album Two", "1982-01-01")]
    album, why = af.choose_from_itunes(items, "Toto", "Africa")
    assert album is None and "tie" in why


def test_itunes_another_artist_never_matches():
    assert (
        af.choose_from_itunes(
            [_it("Rosanna", "Cover Album", "2001-01-01", artist="Tribute Band")], "Toto", "Rosanna"
        )[0]
        is None
    )


def test_the_stage_asks_itunes_last_and_only_for_what_is_left(env, monkeypatch):
    monkeypatch.setattr(af, "mb_confirm", lambda a, al, t: (True, "mb ok"))
    p = env.add("Africa")
    env.answers["acoustid"] = []
    env.answers["discogs"] = []
    env.answers["deezer"] = []
    env.answers["itunes"] = [_it("Africa", "Toto IV", "1982-03-01")]
    env.run()
    assert env.album(p)[0] == "Toto IV" and env.calls["itunes"] == ["Africa"]
    ev = env.conn.execute("SELECT note FROM events WHERE event_type='ALBUM_FILLED'").fetchone()[0]
    assert ev.startswith("itunes:")


def test_a_live_or_version_candidate_title_is_not_the_same_recording():
    """norm() strips brackets, so "Rosanna (Live)" normalised equal to "Rosanna" and the
    live album could be returned for the studio song (found 2026-10-03)."""
    live = {
        "artist": {"name": "Toto"},
        "title": "Rosanna (Live)",
        "album": {"title": "Absolutely Live"},
    }
    assert af.choose_from_deezer([live], "Toto", "Rosanna")[0] is None
    assert (
        af.choose_from_itunes(
            [_it("Rosanna (Live)", "Absolutely Live", "1993-10-12", count=8)], "Toto", "Rosanna"
        )[0]
        is None
    )


# ── MusicBrainz confirmation: is this song really on that official studio album? ──


def _rec(title="Rosanna", artist="Toto", score=100, releases=None):
    return {
        "title": title,
        "score": score,
        "artist-credit": [{"name": artist}],
        "releases": releases or [],
    }


def _rel(title, status="Official", date="1982", primary="Album", secondary=None):
    rg = {"primary-type": primary, "secondary-types": secondary or []}
    return {"title": title, "status": status, "date": date, "release-group": rg}


def test_musicbrainz_confirms_an_official_studio_album():
    ok, why = af.choose_mb_confirmation(
        [_rec(releases=[_rel("Toto IV")])], "Toto", "Rosanna", "Toto IV"
    )
    assert ok and "Toto IV" in why and "1982" in why


@pytest.mark.parametrize(
    "recs",
    [
        [_rec(releases=[_rel("Toto IV", status="Bootleg")])],
        [_rec(releases=[_rel("Toto IV", secondary=["Compilation"])])],
        [_rec(releases=[_rel("Toto IV", primary="Single")])],
        [_rec(releases=[_rel("Tambu")])],
        [_rec(score=56, releases=[_rel("Toto IV")])],
        [_rec(artist="Tribute Band", releases=[_rel("Toto IV")])],
        [_rec(title="Africa", releases=[_rel("Toto IV")])],
        [],
    ],
)
def test_musicbrainz_does_not_confirm_anything_less(recs):
    assert af.choose_mb_confirmation(recs, "Toto", "Rosanna", "Toto IV")[0] is False


@pytest.mark.parametrize("mb_title", ["For Those About to Rock We Salute You",
                                      "For Those About to Rock (We Salute You)"])  # fmt: skip
def test_words_in_brackets_on_one_side_only_still_name_the_same_album(mb_title):
    """Batch 3 (2026-10-25): AC/DC's arriving "For Those About To Rock (We Salute
    You)" was cleared. Brackets were dropped with their words, and MusicBrainz
    writes the album without brackets, so "for those about to rock" never met
    "for those about to rock we salute you"."""
    name = "For Those About To Rock (We Salute You)"
    recs = [_rec(title=mb_title, artist="AC/DC", releases=[_rel(mb_title, date="1981")])]
    assert af.choose_mb_confirmation(recs, "AC/DC", name, name)[0] is True


def test_a_bracketed_word_is_still_not_another_album():
    recs = [_rec(releases=[_rel("Toto IV Live")])]
    assert af.choose_mb_confirmation(recs, "Toto", "Rosanna", "Toto IV")[0] is False


def test_an_album_named_live_is_never_a_studio_answer():
    """MusicBrainz lists "Forever Gold (Live)" as a plain Album, so the name is filtered too."""
    for album in ("Forever Gold (Live)", "Absolutely Live", "Live at the Roxy 1981"):
        assert (
            af.choose_from_itunes(
                [_it("Rock This Town", album, "2002-12-31", artist="Stray Cats")],
                "Stray Cats",
                "Rock This Town",
            )[0]
            is None
        )
        dz = {
            "artist": {"name": "Stray Cats"},
            "title": "Rock This Town",
            "album": {"title": album},
        }
        assert af.choose_from_deezer([dz], "Stray Cats", "Rock This Town")[0] is None


def test_the_stage_takes_an_itunes_album_only_if_musicbrainz_confirms(env, monkeypatch):
    p_yes, p_no = env.add("Africa"), env.add("Hold The Line")
    env.answers["acoustid"] = []
    env.answers["discogs"] = []
    env.answers["deezer"] = []
    env.answers["itunes"] = lambda t: [_it(t, "Toto IV" if t == "Africa" else "Toto", "1982-03-01")]
    asked = []
    monkeypatch.setattr(
        af, "mb_confirm", lambda a, al, t: asked.append(al) or (t == "Africa", f"mb:{t}")
    )
    env.run()
    assert env.album(p_yes)[0] == "Toto IV"
    assert env.album(p_no)[0] is None and env.album(p_no)[1], (
        "unconfirmed: left empty, and stamped as asked"
    )
    assert sorted(asked) == ["Toto", "Toto IV"]


# ── Discogs as a second opinion on an album another source proposed ──


def _dg(title, formats=("Vinyl", "Album"), year="1982"):
    return {"title": title, "format": list(formats), "year": year}


def test_discogs_confirms_an_album_release_by_the_artist():
    ok, why = af.choose_discogs_confirmation([_dg("Toto - Toto IV")], "Toto", "Toto IV")
    assert ok and "Toto IV" in why and "1982" in why


@pytest.mark.parametrize(
    "results",
    [
        [_dg("Toto - Toto IV", formats=("Vinyl", "Single"))],
        [_dg("Toto - Toto IV", formats=("CD", "Compilation"))],
        [_dg("Toto - Toto IV", formats=("CD", "Album", "Unofficial Release"))],
        [_dg("Tribute Band - Toto IV")],
        [_dg("Toto - Tambu")],
        [{"title": "no dash", "format": ["Album"]}],
        [],
    ],
)
def test_discogs_does_not_confirm_anything_less(results):
    assert af.choose_discogs_confirmation(results, "Toto", "Toto IV")[0] is False


# ── An album a song arrives with: checked with MusicBrainz (Grey, 2026-10-23) ──


def _verified(env, path):
    return env.conn.execute(
        "SELECT album_verified_at FROM archive WHERE file_path=?", (path,)
    ).fetchone()[0]


def test_an_arriving_album_musicbrainz_confirms_is_kept(env):
    env.run()  # the column arrives; what was there before is not checked
    p = env.add("Africa", album="Toto IV")
    env.answers["confirm"] = {"Toto IV"}
    env.run()
    assert env.calls["confirm"] == ["Toto IV"] and len(env.calls["acoustid"]) == 1
    assert env.album(p)[0] == "Toto IV" and _verified(env, p)


def test_an_arriving_compilation_is_cleared_and_looked_up_in_the_same_run(env):
    """The Odds & Ends songs arrived as "70s Hits" and "Greatest Hits" (2026-10-23)."""
    env.run()
    p = env.add("Africa", album="70s Hits")
    env.answers["confirm"] = {"Toto IV"}
    env.answers["acoustid"] = [_rg("r1", "Toto IV")]
    env.run()
    assert env.album(p)[0] == "Toto IV"
    ev = env.conn.execute(
        "SELECT old_value FROM events WHERE event_type='ALBUM_CLEARED' AND file_path=?", (p,)
    ).fetchone()
    assert ev[0] == "70s Hits"


def test_albums_in_the_catalogue_before_the_check_are_left_alone(tmp_path):
    """open_db adds the column before any intake and stamps the albums already
    there, so they are never checked (Codex review of #229, round 2)."""
    db = tmp_path / "old.db"
    conn = open_db(db)
    conn.execute("ALTER TABLE archive DROP COLUMN album_verified_at")  # a catalogue from before
    upsert_archive(conn, {"file_path": "/x/a.m4a", "status": "CATALOGUED",
                          "artist": "Toto", "title": "Rosanna", "album": "Greatest Hits"})  # fmt: skip
    conn.commit()
    conn.close()
    conn = open_db(db)
    stamp = conn.execute("SELECT album_verified_at FROM archive").fetchone()[0]
    assert stamp and stamp.startswith("before the arrival check")
    n, _ = AlbumFillStage.plan_candidates(conn, None)
    assert n == 0, "an album from before the check is not one to check"


def test_no_answer_leaves_the_arriving_album_for_the_next_run(env, monkeypatch):
    env.run()
    p = env.add("Africa", album="70s Hits")

    def _down(*_, **__):
        raise Unavailable("503")

    monkeypatch.setattr(af, "mb_confirm", _down)
    env.run()
    assert env.album(p)[0] == "70s Hits" and not _verified(env, p)


def test_classical_and_version_titles_are_not_checked(env):
    env.run()
    live = env.add("Rosanna (Live)", album="Absolutely Live")
    bach = env.add("Air", artist="Johann Sebastian Bach", album="Bach: Greatest Hits")
    env.conn.execute("UPDATE archive SET genre='Classical' WHERE file_path=?", (bach,))
    env.conn.commit()
    env.answers["confirm"] = set()
    env.run()
    assert env.calls["confirm"] == []
    assert env.album(live)[0] == "Absolutely Live" and env.album(bach)[0] == "Bach: Greatest Hits"


def test_an_arrival_never_finalized_is_still_checked(env):
    """Held back by Finalize, or catalogued by a run that stopped and resumed:
    an arrival is told from an old album by when the catalogue was opened, not
    by finalized_at (Codex review of #229, round 2)."""
    new = env.add("Africa", album="70s Hits")
    env.answers["confirm"] = set()
    env.run()
    assert env.calls["confirm"] == ["70s Hits"] and env.album(new)[0] is None


def test_the_preview_counts_arriving_albums_to_check(env):
    env.run()
    env.add("Africa", album="70s Hits")
    n, what = AlbumFillStage.plan_candidates(env.conn, env.cfg)
    assert n == 1 and "arriving album" in what


def test_the_tagger_removes_an_album_the_check_cleared():
    """Left in the tag, a rebuild from the files would bring it back (Codex review of #229)."""
    from musaeus.stages.tagger import TaggerStage

    cleared = {"album": None, "album_verified_at": f"{af.CLEARED_BY_CHECK} (2026-10-23)"}
    assert TaggerStage()._compute_changes(cleared, {"album": "Greatest Hits"}).get("album") == ""
    kept = {"album": None, "album_verified_at": None}
    assert "album" not in TaggerStage()._compute_changes(kept, {"album": "Greatest Hits"})


def test_a_title_with_live_as_a_word_is_still_checked(env):
    """Only a version LABEL is skipped -- "(Live)" -- not the word in a title
    (Codex review of #229, round 3)."""
    p = env.add("Live and Let Die", artist="Wings", album="Greatest Hits")
    env.answers["confirm"] = {"Red Rose Speedway"}
    env.run()
    assert env.calls["confirm"] == ["Greatest Hits"] and env.album(p)[0] is None


def test_a_clear_counts_as_a_change_and_is_verified(env):
    p = env.add("Africa", album="70s Hits")
    env.answers["confirm"] = set()
    result, ctx = env.run()
    assert result.files_changed == 1 and env.album(p)[0] is None
    assert AlbumFillStage().verify_effect(ctx, result) == []
    env.conn.execute("UPDATE archive SET album='70s Hits' WHERE file_path=?", (p,))
    assert AlbumFillStage().verify_effect(ctx, result), "the rejected name came back unnoticed"


def test_a_cleared_album_filled_back_by_a_source_is_verified(env):
    """AcoustID found Santana's "Shaman" after the text check could not; the
    self-check flagged it as the rejected album come back (2026-10-23)."""
    p = env.add("Africa", album="Toto IV")
    env.answers["confirm"] = set()
    env.answers["acoustid"] = [_rg("r1", "Toto IV")]
    result, ctx = env.run()
    assert env.album(p)[0] == "Toto IV"
    assert AlbumFillStage().verify_effect(ctx, result) == []


def test_an_edit_keeps_its_arriving_album_and_is_not_searched_by_text(env):
    """An "(Edit)" is its own cut: neither the arrival check nor a text search
    may give it the studio album by name (Codex review of #232). "Original"
    is not a version here: the original is the studio recording."""
    p = env.add("Rosanna (Edit)", album="Rosanna - Single")
    q = env.add("Africa (Edit)")
    env.answers["confirm"] = set()
    env.answers["acoustid"] = []
    env.answers["deezer"] = [
        {"artist": {"name": "Toto"}, "title": "Africa", "album": {"title": "Toto IV"}}
    ]
    env.run()
    assert env.calls["confirm"] == [] and env.calls["deezer"] == []
    assert env.album(p)[0] == "Rosanna - Single" and env.album(q)[0] is None


def test_edit_as_a_word_of_the_title_is_not_a_version(env):
    """ "Edit the Sad Parts" is a song: searched by text like any other (Codex review of #232)."""
    q = env.add("Edit the Sad Parts", artist="Fiona")
    env.answers["acoustid"] = []
    env.answers["deezer"] = [
        {
            "artist": {"name": "Fiona"},
            "title": "Edit the Sad Parts",
            "album": {"title": "Studio LP"},
        }
    ]
    env.run()
    assert env.calls["deezer"] == ["Edit the Sad Parts"] and env.album(q)[0] == "Studio LP"


@pytest.mark.parametrize(
    ("title", "version"),
    [("Song (Edit)", True), ("Song (Single Edit)", True), ("Song - Edit", True),
     ("Song (Edit the Script)", False), ("Edit the Sad Parts", False)],
)  # fmt: skip
def test_edit_is_a_version_only_as_a_whole_label(title, version):
    """Codex review of #232, round 3: "(Edit the Script)" is part of the title."""
    assert af._names_a_version(title) is version


def test_a_live_recording_keeps_its_live_album(env):
    """Grey, 2026-10-23: Hendrix's live "Tax Free" arrived on "Miami Pop Festival";
    the studio-only check rejected it and a text search gave it "War Heroes"."""
    p = env.add("Tax Free", artist="Jimi Hendrix", album="Miami Pop Festival")
    env.answers["confirm"] = set()
    env.answers["acoustid"] = [_rg("m", "Miami Pop Festival", "Jimi Hendrix", secondary=["Live"])]
    env.run()
    assert env.calls["confirm"] == [], "the fingerprint settled it"
    assert env.album(p)[0] == "Miami Pop Festival"


def test_a_live_recording_is_never_given_a_studio_album_by_name(env):
    p = env.add("Tax Free", artist="Jimi Hendrix")
    env.answers["acoustid"] = [_rg("m", "Miami Pop Festival", "Jimi Hendrix", secondary=["Live"])]
    env.answers["discogs"] = [
        {"title": "Jimi Hendrix - War Heroes", "format": ["LP", "Album"], "year": "1972"}
    ]
    env.run()
    assert env.calls["discogs"] == [] and env.album(p)[0] is None


def test_a_compilation_the_recording_is_on_is_still_not_kept(env):
    p = env.add("Africa", album="70s Hits")
    env.answers["confirm"] = set()
    env.answers["acoustid"] = [_rg("c", "70s Hits", secondary=["Compilation"])]
    env.run()
    assert env.album(p)[0] != "70s Hits"


def test_a_classical_work_is_not_checked(env):
    """Horowitz's "Mozart: Piano Concerto No. 23 ... K. 488" lost its album (2026-10-23)."""
    p = env.add("Mozart: Piano Concerto No. 23 In A Major, K. 488: II. Adagio", artist="Vladimir Horowitz",
                album="Mozart: Piano Concerto No. 23")  # fmt: skip
    env.answers["confirm"] = set()
    env.run()
    assert env.calls["confirm"] == [] and env.album(p)[0] == "Mozart: Piano Concerto No. 23"


def test_a_fingerprint_failure_still_lets_musicbrainz_check_the_album(env):
    """Codex review of #233: an AcoustID timeout skipped MusicBrainz as well."""
    p = env.add("Africa", album="Toto IV")
    env.answers["acoustid"] = Unavailable("timeout")
    env.answers["confirm"] = {"Toto IV"}
    env.run()
    assert env.calls["confirm"] == ["Toto IV"] and env.album(p)[0] == "Toto IV"


def test_a_fingerprint_failure_and_a_musicbrainz_no_keep_the_album_for_next_run(env):
    """Codex review of #233, round 3: with AcoustID down, the studio-only search
    said no to "Miami Pop Festival" and the live album was cleared."""
    p = env.add("Tax Free", artist="Jimi Hendrix", album="Miami Pop Festival")
    env.answers["acoustid"] = Unavailable("timeout")
    env.answers["confirm"] = set()
    env.run()
    stamp = env.conn.execute(
        "SELECT album_verified_at FROM archive WHERE file_path=?", (p,)
    ).fetchone()[0]
    assert env.album(p)[0] == "Miami Pop Festival" and stamp is None, "asked again next run"


def test_a_live_recording_tagged_with_a_studio_album_is_not_confirmed_by_title(env):
    """Codex review of #233, round 4: the fingerprint knew the live "Tax Free",
    and a title search still confirmed the studio "War Heroes" it arrived with."""
    p = env.add("Tax Free", artist="Jimi Hendrix", album="War Heroes")
    env.answers["acoustid"] = [_rg("m", "Miami Pop Festival", "Jimi Hendrix", secondary=["Live"])]
    env.answers["confirm"] = {"War Heroes"}
    env.run()
    assert env.album(p)[0] != "War Heroes"


def test_with_no_fingerprint_answer_a_live_album_still_confirms(env):
    """Codex review of #233, round 4: AcoustID knew no release for the live take,
    and the studio-only search cleared "Miami Pop Festival"."""
    p = env.add("Tax Free", artist="Jimi Hendrix", album="Miami Pop Festival")
    env.answers["acoustid"] = []
    env.answers["confirm"] = set()
    env.answers["confirm_live"] = {"Miami Pop Festival"}
    env.run()
    assert env.album(p)[0] == "Miami Pop Festival"


@pytest.mark.parametrize(("live_ok", "confirmed"), [(False, False), (True, True)])
def test_musicbrainz_counts_a_live_album_only_when_asked(live_ok, confirmed):
    rec = {"score": 100, "title": "Tax Free", "artist-credit": [{"name": "Jimi Hendrix"}],
           "releases": [{"title": "Miami Pop Festival", "status": "Official", "date": "2013",
                         "release-group": {"primary-type": "Album", "secondary-types": ["Live"]}}]}  # fmt: skip
    ok, _ = af.choose_mb_confirmation(
        [rec], "Jimi Hendrix", "Tax Free", "Miami Pop Festival", live_ok=live_ok
    )
    assert ok is confirmed


def test_a_live_take_also_on_a_compilation_is_still_live(env):
    p = env.add("Tax Free", artist="Jimi Hendrix")
    env.answers["acoustid"] = [
        _rg("m", "Miami Pop Festival", "Jimi Hendrix", secondary=["Live"]),
        _rg("c", "Hendrix Best Of", "Jimi Hendrix", secondary=["Compilation"]),
    ]
    env.answers["discogs"] = [
        {"title": "Jimi Hendrix - War Heroes", "format": ["LP", "Album"], "year": "1972"}
    ]
    env.run()
    assert env.calls["discogs"] == [] and env.album(p)[0] is None


@pytest.mark.parametrize("secondary", [["Remix"], ["DJ-mix"]])
def test_a_remix_or_dj_mix_album_is_not_confirmed_by_the_fingerprint(env, secondary):
    env.add("Africa", album="Africa Remixed")
    env.answers["acoustid"] = [_rg("r", "Africa Remixed", secondary=secondary)]
    env.answers["confirm"] = set()
    env.run()
    assert env.calls["confirm"] == ["Africa Remixed"], "only MusicBrainz can confirm it"


def test_a_known_composers_name_before_a_colon_is_classical(env):
    """Grey, 2026-10-23, option B: "Mozart: ..." with Mozart a known composer."""
    env.cfg.meta_dir.mkdir(parents=True, exist_ok=True)
    (env.cfg.meta_dir / "Composer_Canon.tsv").write_text(
        "Wolfgang Amadeus Mozart\tWolfgang Amadeus Mozart\n"
    )
    p = env.add("Mozart: Piano Concerto No. 23 in A major: II. Adagio", artist="Vladimir Horowitz",
                album="Mozart Concertos")  # fmt: skip
    q = env.add("Bach: Goldberg Variations: Aria", artist="Glenn Gould", album="Goldberg")
    env.conn.execute(
        "INSERT INTO archive (file_path, status, artist, genre) VALUES ('/c', 'CATALOGUED', 'Johann Sebastian Bach', 'Classical')"
    )
    env.conn.commit()
    env.answers["confirm"] = set()
    env.run()
    assert env.calls["confirm"] == []
    assert env.album(p)[0] == "Mozart Concertos" and env.album(q)[0] == "Goldberg"


def test_a_form_word_alone_is_not_classical(env):
    """ "Symphony of Destruction" is Megadeth's: its arriving album is checked."""
    env.add("Symphony of Destruction", artist="Megadeth", album="Greatest Hits")
    env.answers["confirm"] = set()
    env.run()
    assert env.calls["confirm"] == ["Greatest Hits"]


@pytest.mark.parametrize(
    ("artist", "searches"), [("Paul McCartney, Wings", 2), ("Earth, Wind & Fire", 1)]
)
def test_a_joint_credit_that_finds_nothing_is_asked_by_its_first_name(
    monkeypatch, artist, searches
):
    """Batch 2 rerun, 2026-10-24: "Paul McCartney, Wings" found no recording at all,
    and "My Love" lost its album "Red Rose Speedway". A credit that finds nothing
    is asked again by its first name; a band name with a comma is found as it is."""
    rec = {"score": 100, "title": "My Love", "artist-credit": [{"name": "Paul McCartney & Wings"}],
           "releases": [{"title": "Red Rose Speedway", "status": "Official", "date": "1973",
                         "release-group": {"primary-type": "Album", "secondary-types": []}}]}  # fmt: skip
    asked = []

    def _mb(url, headers=None):
        query = parse_qs(urlparse(url).query)["query"][0]
        asked.append(query)
        whole = 'artist:"Earth, Wind & Fire"' in query or 'artist:"Paul McCartney"' in query
        return {"recordings": [rec] if whole else []}

    monkeypatch.setattr(af.time, "sleep", lambda *_: None)
    monkeypatch.setattr(af, "_get_json", _mb)
    if artist.startswith("Earth"):
        rec["artist-credit"] = [{"name": "Earth, Wind & Fire"}]
    ok, _ = af.mb_confirm(artist, "Red Rose Speedway", "My Love")
    assert ok and len(asked) == searches


def _rel_on(title, secondary=None, status="Official", date="1981"):
    return {"title": title, "status": status, "date": date,
            "release-group": {"primary-type": "Album", "secondary-types": secondary or []}}  # fmt: skip


def test_words_in_brackets_are_asked_again_as_plain_words(monkeypatch):
    """Batch 3 rerun #3 (2026-10-25, MusicBrainz's real answers): asked with
    "(We Salute You)" dropped, MusicBrainz gave AC/DC's song only on
    compilations, or with a score of 76; asked with the words kept, it gave the
    recording at 100 on the official album, and the album was not cleared."""
    name = "For Those About To Rock (We Salute You)"
    stripped = [
        {"score": 100, "title": "For Those About to Rock", "artist-credit": [{"name": "AC/DC"}],
         "releases": [_rel_on("For Those About to Rock", ["Compilation"])]},
        # Really 76; below the cut-off here, so that the retry is what confirms.
        {"score": 60, "title": "For Those About to Rock (We Salute You)", "artist-credit": [{"name": "AC/DC"}],
         "releases": [_rel_on("For Those About To Rock", date="2009")]},
    ]  # fmt: skip
    whole = [dict(stripped[1], score=100)]
    asked = []

    def _mb(url, headers=None):
        query = parse_qs(urlparse(url).query)["query"][0]
        asked.append(query)
        return {"recordings": whole if "We Salute You" in query else stripped}

    monkeypatch.setattr(af.time, "sleep", lambda *_: None)
    monkeypatch.setattr(af, "_get_json", _mb)
    ok, why = af.mb_confirm("AC/DC", name, name)
    assert ok and len(asked) == 2, why


def test_the_studio_recording_ranked_below_a_compilation_still_confirms():
    """Batch 5 (2026-10-25, MusicBrainz's real answer): Stevie Wonder's "Living For
    The City" on "Innervisions" was cleared. The recording on the studio album
    scored 76, behind one only on a compilation (100), and 90 was required --
    though title, artist and album must all match exactly anyway. AC/DC's scored
    76 too."""
    recs = [
        _rec(title="Living for the City", artist="Stevie Wonder",
             releases=[_rel_on("Talking Book + Innervisions", ["Compilation"])]),
        _rec(title="Living for the City", artist="Stevie Wonder", score=76,
             releases=[_rel_on("The Definitive Collection", ["Compilation"]), _rel_on("Innervisions", date="1973")]),
    ]  # fmt: skip
    assert af.choose_mb_confirmation(recs, "Stevie Wonder", "Living For The City", "Innervisions")[
        0
    ]


def test_a_name_without_brackets_is_asked_once(monkeypatch):
    asked = []

    def _mb(url, headers=None):
        asked.append(url)
        return {"recordings": []}

    monkeypatch.setattr(af.time, "sleep", lambda *_: None)
    monkeypatch.setattr(af, "_get_json", _mb)
    assert af.mb_confirm("Toto", "Toto IV", "Rosanna")[0] is False and len(asked) == 1


def test_a_retry_by_first_name_needs_every_name_of_the_credit(monkeypatch):
    """Codex review of #235 (reproduced): "Earth, Wind & Fire" found nothing, the
    retry asked "Earth", and a recording by the band Earth confirmed the album."""
    earth = {"score": 100, "title": "Fantasy", "artist-credit": [{"name": "Earth"}],
             "releases": [{"title": "All 'N All", "status": "Official", "date": "1977",
                           "release-group": {"primary-type": "Album", "secondary-types": []}}]}  # fmt: skip

    def _mb(url, headers=None):
        query = parse_qs(urlparse(url).query)["query"][0]
        return {"recordings": [earth] if 'artist:"Earth"' in query else []}

    monkeypatch.setattr(af.time, "sleep", lambda *_: None)
    monkeypatch.setattr(af, "_get_json", _mb)
    assert af.mb_confirm("Earth, Wind & Fire", "All 'N All", "Fantasy")[0] is False


def _credit_rec(credit, title="Song", album="Album"):
    return {"score": 100, "title": title, "artist-credit": [{"name": credit}],
            "releases": [{"title": album, "status": "Official", "date": "2000",
                          "release-group": {"primary-type": "Album", "secondary-types": []}}]}  # fmt: skip


@pytest.mark.parametrize(
    ("credit", "full", "retry", "confirmed"),
    [
        # a solo "John Lennon Paul McCartney" is not the joint "John Lennon, Paul McCartney"
        ("John Lennon, Paul McCartney", [], [_credit_rec("John Lennon Paul McCartney")], False),
        # a stray first hit does not skip the retry
        (
            "Paul McCartney, Wings",
            [_credit_rec("Paul McCartney & Wings", title="Other Song")],
            [_credit_rec("Paul McCartney & Wings")],
            True,
        ),
    ],
)
def test_a_retry_names_the_same_acts_and_is_not_skipped_by_a_stray_hit(
    monkeypatch, credit, full, retry, confirmed
):
    """Codex review of #235, round 2 (both reproduced)."""
    monkeypatch.setattr(af.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        af,
        "_get_json",
        lambda url, headers=None: {
            "recordings": full
            if f'artist:"{credit}"' in parse_qs(urlparse(url).query)["query"][0]
            else retry
        },
    )
    assert af.mb_confirm(credit, "Album", "Song")[0] is confirmed


def test_a_band_named_with_commas_is_never_split_into_acts(monkeypatch):
    """Codex review of #235, round 3 (reproduced): "Earth, Wind & Fire" was confirmed
    by a recording credited to three acts named Earth, Wind and Fire."""
    asked = []
    three = _credit_rec("Earth & Wind & Fire")

    def _mb(url, headers=None):
        asked.append(url)
        return {"recordings": [] if len(asked) == 1 else [three]}

    monkeypatch.setattr(af.time, "sleep", lambda *_: None)
    monkeypatch.setattr(af, "_get_json", _mb)
    assert af.mb_confirm("Earth, Wind & Fire", "Album", "Song")[0] is False and len(asked) == 1
