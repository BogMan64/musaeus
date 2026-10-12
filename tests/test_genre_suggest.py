"""`musaeus genre-suggest` (Grey, 2026-10-11): MusicBrainz and Wikipedia
suggest, Grey decides, MasterLaw changes only for the rows Grey approved."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from musaeus import genre_suggest as gs
from musaeus import network_policy
from musaeus.canon.genre_law import GenreLaw
from musaeus.db import ensure_columns, open_db, upsert_archive
from musaeus.network_policy import NetworkPolicy

WIKITEXT = """{{Infobox musical artist
| name = Fleetwood Mac
| genre = {{hlist|[[Rock music|Rock]]|[[pop rock]]|[[blues rock]]<ref>{{cite web|url=x}}</ref>|<!-- not this -->[[soft rock]]}}
| years_active = 1967–present
}}
'''Fleetwood Mac''' are a band."""

FLATLIST = """{{Infobox musical artist
| genre       = {{flatlist|
* [[Folk music|Folk]]
* [[folk rock]]
* [[Singer-songwriter]]
}}
| label = Elektra
}}"""


@pytest.fixture
def vocab(tmp_path: Path) -> gs.Vocabulary:
    allowed = tmp_path / "Genre_Allowed.txt"
    allowed.write_text(
        "# vocabulary\nRock\nPop\nFolk\nFolk Rock\nSoft Rock\nBlues Rock\nSinger-Songwriter\n"
        "R&B/Funk/Soul\nClassic Pop\n",
        encoding="utf-8",
    )
    mapping = tmp_path / "Genre_Canonical_Map.txt"
    mapping.write_text("pop rock => Pop\nsoul => R&B/Funk/Soul\n", encoding="utf-8")
    return gs.Vocabulary(allowed, mapping)


class TestTheInfobox:
    def test_an_hlist_with_links_refs_and_comments(self):
        assert gs.infobox_genres(WIKITEXT) == ["Rock", "pop rock", "blues rock", "soft rock"]

    def test_a_flatlist(self):
        assert gs.infobox_genres(FLATLIST) == ["Folk", "folk rock", "Singer-songwriter"]

    def test_no_genre_field(self):
        assert gs.infobox_genres("{{Infobox person\n| name = X\n}}") == []


class TestTheVocabulary:
    def test_exact_and_mapped_in_any_case(self, vocab):
        assert vocab.resolve("rock") == "Rock"
        assert vocab.resolve("Rock music") == "Rock"
        assert vocab.resolve("POP ROCK") == "Pop"
        assert vocab.resolve("singer songwriter") == "Singer-Songwriter"

    def test_never_a_fuzzy_guess(self, vocab):
        # GenreCanon would fuzz "art pop" toward "Pop"; a suggestion must not.
        assert vocab.resolve("art pop") is None
        assert vocab.resolve("rocks") is None

    def test_a_typed_genre_is_written_as_listed(self, vocab):
        assert vocab.canonical("folk rock") == "Folk Rock"
        assert vocab.canonical("Folk Rokc") is None


class TestOneSuggestion:
    def test_both_agree(self, vocab):
        s = gs.suggest(
            gs.Found(musicbrainz=[("rock", 9), ("pop rock", 3)], wikipedia=["Rock"]), vocab
        )
        assert (s.genre, s.agreement) == ("Rock", "both agree")

    def test_they_differ_and_both_are_shown(self, vocab):
        s = gs.suggest(gs.Found(musicbrainz=[("pop rock", 5)], wikipedia=["Rock"]), vocab)
        assert (s.genre, s.agreement) == ("Pop", "differ: Wikipedia says Rock")

    def test_votes_add_up_across_spellings(self, vocab):
        found = gs.Found(musicbrainz=[("rock", 4), ("pop", 3), ("pop rock", 3)])
        assert gs.suggest(found, vocab).genre == "Pop"

    def test_what_does_not_map_is_listed(self, vocab):
        s = gs.suggest(gs.Found(musicbrainz=[("yacht rock", 2)], wikipedia=["Art pop"]), vocab)
        assert s.genre is None and s.unmapped == ["yacht rock", "Art pop"]


def _db(tmp_path: Path):
    conn = open_db(tmp_path / "m.db")
    ensure_columns(conn, (("mb_artist_id", "TEXT"), ("mb_artist_name", "TEXT")))
    rows = [
        ("Fleetwood Mac", "mbid-fm", 3),
        ("Judy Collins", None, 2),
        ("Carole King", "mbid-ck", 1),
        ("Carole King, James Taylor", None, 1),  # a credit: follows its lead
    ]
    n = 0
    for artist, mbid, songs in rows:
        for _ in range(songs):
            n += 1
            upsert_archive(conn, {"file_path": f"/m/{n}.m4a", "status": "CATALOGUED",
                                  "artist": artist, "title": f"t{n}"})  # fmt: skip
            conn.execute(
                "UPDATE archive SET mb_artist_id = ? WHERE file_path = ?", (mbid, f"/m/{n}.m4a")
            )
    conn.commit()
    return conn


def _law(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "MasterLaw.csv"
    p.write_bytes(text.encode("utf-8"))
    return p


ANSWERS = {
    "Fleetwood Mac": gs.Found("mbid-fm", [("soft rock", 6), ("rock", 2)], ["Rock", "soft rock"]),
    "Judy Collins": gs.Found("mbid-jc", [("folk", 7)], ["Folk"]),
    "Carole King": gs.Found("mbid-ck", [("pop", 3)], ["Pop", "soft rock"]),
}


def _read(sheet: Path) -> list[dict]:
    with open(sheet, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


class TestGenerate:
    def test_the_sheet_holds_only_what_differs_from_the_law(self, tmp_path, vocab):
        conn = _db(tmp_path)
        law = GenreLaw(_law(tmp_path, "artist,genre\r\nFleetwood Mac,Rock\r\nCarole King,Pop\r\n"))
        asked: list[tuple[str, str | None]] = []

        def lookup(artist, mbid):
            asked.append((artist, mbid))
            return ANSWERS[artist]

        sheet = tmp_path / "Desktop" / "sheet.csv"
        rep = gs.generate(conn, law, vocab, tmp_path / "cache.json", sheet, lookup=lookup)

        assert sorted(asked) == [("Carole King", "mbid-ck"), ("Fleetwood Mac", "mbid-fm"),
                                 ("Judy Collins", None)]  # fmt: skip
        rows = {r["artist"]: r for r in _read(sheet)}
        assert set(rows) == {"Fleetwood Mac", "Judy Collins"}  # Carole King agrees
        assert rows["Fleetwood Mac"]["current_genre"] == "Rock"
        assert rows["Fleetwood Mac"]["suggested_genre"] == "Soft Rock"
        assert rows["Fleetwood Mac"]["agreement"] == "differ: Wikipedia says Rock"
        assert rows["Judy Collins"]["current_genre"] == ""
        assert rows["Judy Collins"]["suggested_genre"] == "Folk"
        assert all(r["approve"] == "" for r in rows.values()), "nothing comes pre-approved"
        assert (rep.agree_with_law, rep.rows) == (1, 2)

    def test_a_second_run_asks_nobody(self, tmp_path, vocab):
        conn = _db(tmp_path)
        law = GenreLaw(_law(tmp_path, "artist,genre\r\n"))
        cache = tmp_path / "cache.json"
        gs.generate(conn, law, vocab, cache, tmp_path / "a.csv", lookup=lambda a, m: ANSWERS[a])

        def no_network(artist, mbid):
            raise AssertionError(f"asked again for {artist}")

        rep = gs.generate(conn, law, vocab, cache, tmp_path / "b.csv", lookup=no_network)
        assert (rep.asked, rep.from_cache) == (0, 3)
        assert _read(tmp_path / "a.csv") == _read(tmp_path / "b.csv")

    def test_no_answer_is_not_remembered(self, tmp_path, vocab):
        conn = _db(tmp_path)
        law = GenreLaw(_law(tmp_path, "artist,genre\r\n"))

        def down(artist, mbid):
            raise gs.LookupUnavailable("timeout")

        rep = gs.generate(conn, law, vocab, tmp_path / "c.json", tmp_path / "s.csv", lookup=down)
        assert (rep.unavailable, rep.asked) == (3, 0)
        assert gs._load_cache(tmp_path / "c.json") == {}

    def test_it_never_writes_the_catalogue(self, tmp_path, vocab):
        conn = _db(tmp_path)
        before = conn.execute("SELECT * FROM archive ORDER BY file_path").fetchall()
        law = GenreLaw(_law(tmp_path, "artist,genre\r\n"))
        gs.generate(conn, law, vocab, tmp_path / "c.json", tmp_path / "s.csv",
                    lookup=lambda a, m: ANSWERS[a])  # fmt: skip
        assert conn.execute("SELECT * FROM archive ORDER BY file_path").fetchall() == before


def test_every_lookup_asks_the_gateway(monkeypatch):
    def no_transport(*a, **k):
        raise AssertionError("urlopen was called under LOCAL_ONLY")

    monkeypatch.setattr(gs, "urlopen", no_transport)
    monkeypatch.setattr(gs, "_MB_PAUSE_S", 0)
    with (
        network_policy.policy(NetworkPolicy.LOCAL_ONLY) as gateway,
        pytest.raises(gs.LookupUnavailable),
    ):
        gs.look_up("Fleetwood Mac", "mbid-fm")
    assert gateway.denials, "the lookup never asked the gateway"


LAW = 'artist,genre\r\nFleetwood Mac,Rock\r\n"Crosby, Stills & Nash",Folk Rock\r\nThe Byrds,Folk Rock\r\n'


def _sheet(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "sheet.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=gs.SHEET_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in gs.SHEET_FIELDS})
    return p


class TestApply:
    def test_only_approved_rows_change_and_the_rest_stays_byte_for_byte(self, tmp_path, vocab):
        law = _law(tmp_path, LAW)
        sheet = _sheet(tmp_path, [
            {"approve": "yes", "artist": "Fleetwood Mac", "suggested_genre": "Soft Rock"},
            {"approve": "", "artist": "Byrds, The", "suggested_genre": "Rock"},
            {"approve": "y", "artist": "Judy Collins", "suggested_genre": "folk"},
        ])  # fmt: skip

        res = gs.apply(sheet, law, vocab)

        assert res.changed == [("Fleetwood Mac", "Rock", "Soft Rock")]
        assert res.added == [("Judy Collins", "Folk")]
        assert law.read_bytes() == (
            b'artist,genre\r\nFleetwood Mac,Soft Rock\r\n"Crosby, Stills & Nash",Folk Rock\r\n'
            b"The Byrds,Folk Rock\r\nJudy Collins,Folk\r\n"
        )
        assert res.backup is not None and res.backup.read_bytes() == LAW.encode()
        assert sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".part")) == []

    def test_an_article_form_finds_its_row(self, tmp_path, vocab):
        law = _law(tmp_path, LAW)
        sheet = _sheet(
            tmp_path, [{"approve": "yes", "artist": "Byrds, The", "suggested_genre": "Rock"}]
        )
        res = gs.apply(sheet, law, vocab)
        assert res.changed == [("The Byrds", "Folk Rock", "Rock")] and res.added == []

    @pytest.mark.parametrize(
        ("typed", "why"), [("Folk Rokc", "not in Genre_Allowed"), ("", "no genre")]
    )
    def test_a_misspelt_or_blank_genre_is_refused(self, tmp_path, vocab, typed, why):
        law = _law(tmp_path, LAW)
        sheet = _sheet(
            tmp_path, [{"approve": "yes", "artist": "Fleetwood Mac", "suggested_genre": typed}]
        )
        res = gs.apply(sheet, law, vocab)
        assert res.refused and why in res.refused[0]
        assert law.read_bytes() == LAW.encode() and res.backup is None

    def test_a_dry_run_writes_nothing(self, tmp_path, vocab):
        law = _law(tmp_path, LAW)
        sheet = _sheet(
            tmp_path, [{"approve": "yes", "artist": "Fleetwood Mac", "suggested_genre": "Pop"}]
        )
        before = sorted(p.name for p in tmp_path.iterdir())
        res = gs.apply(sheet, law, vocab, dry_run=True)
        assert res.changed == [("Fleetwood Mac", "Rock", "Pop")]
        assert law.read_bytes() == LAW.encode()
        assert sorted(p.name for p in tmp_path.iterdir()) == before
