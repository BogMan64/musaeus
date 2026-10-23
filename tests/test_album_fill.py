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

    def _confirm(artist, album, title):
        calls["confirm"].append(album)
        yes = answers["confirm"]
        return (yes is True or album in yes), "mb"  # type: ignore[operator]

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
    assert env.album(p)[0] == "River of Dreams"


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
    assert env.calls["confirm"] == ["Toto IV"] and env.calls["acoustid"] == []
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


def test_albums_in_the_catalogue_before_the_check_are_left_alone(env):
    p = env.add("Rosanna", album="Greatest Hits")
    env.answers["confirm"] = set()
    env.run()
    assert env.calls["confirm"] == [] and env.album(p)[0] == "Greatest Hits"


def test_no_answer_leaves_the_arriving_album_for_the_next_run(env, monkeypatch):
    env.run()
    p = env.add("Africa", album="70s Hits")

    def _down(*_):
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


def test_the_first_runs_own_arrivals_are_checked_not_stamped_as_before(env):
    """Scholar catalogues a run's arrivals before this stage adds its column:
    they must not be stamped as old albums (Codex review of #229)."""
    old = env.add("Rosanna", album="Greatest Hits")
    new = env.add("Africa", album="70s Hits")
    env.conn.execute(
        "UPDATE archive SET finalized_at='2999-01-01 00:00:00' WHERE file_path=?", (new,)
    )
    env.conn.commit()
    env.answers["confirm"] = set()
    env.run()
    assert env.calls["confirm"] == ["70s Hits"]
    assert env.album(old)[0] == "Greatest Hits" and env.album(new)[0] is None


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
