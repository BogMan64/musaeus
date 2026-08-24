"""
MusicBrainz queries must not be pre-encoded.

_mb_get URL-encodes the whole query string once via urlencode(). Any
quote() applied while building the Lucene query encodes it a second time,
so "Dusty Springfield" reached MusicBrainz as the literal term
"Dusty%20Springfield" and matched nothing.

Confirmed live 2026-08-23, before the fix:
    _search_artist("Abba")              -> ('d87e52c5-...', 'ABBA')
    _search_artist("Cher")              -> ('bfcc6d75-...', 'Cher')
    _search_artist("Dusty Springfield") -> None
Single-word names worked because encoding them changes nothing — which is
why the defect survived: it failed on almost everything while appearing to
work on whatever anyone spot-checked.

This is silent-no-op number nine. The check that catches it has to look at
the string sent, not at whether a call was made.
"""

from __future__ import annotations

import pytest

from musaeus.stages import mb_enrich


@pytest.fixture
def sent(monkeypatch):
    """Capture the query strings handed to _mb_get."""
    captured: list[str] = []

    def fake(path, params):
        captured.append(params.get("query", ""))
        return {}

    monkeypatch.setattr(mb_enrich, "_mb_get", fake)
    return captured


class TestArtistSearchEncoding:
    @pytest.mark.parametrize(
        "name",
        [
            "Dusty Springfield",
            "The Beach Boys",
            "Urge Overkill",
            "Spirit of the West",
            "Silly Wizard",
        ],
    )
    def test_spaces_survive_as_spaces(self, name, sent):
        mb_enrich._search_artist(name)
        assert sent == [f'artist:"{name}"']
        assert "%20" not in sent[0]

    def test_single_word_name_is_unaffected(self, sent):
        # The case that masked the bug for as long as it existed.
        mb_enrich._search_artist("Cher")
        assert sent == ['artist:"Cher"']


class TestReleaseSearchEncoding:
    def test_album_title_is_not_pre_encoded(self, sent):
        mb_enrich._search_release("mbid-1234", "Pulp Fiction - Music From the Motion Picture")
        assert "%20" not in sent[0]
        assert 'release:"Pulp Fiction - Music From the Motion Picture"' in sent[0]

    def test_empty_album_short_circuits_before_any_request(self, sent):
        assert mb_enrich._search_release("mbid-1234", "") is None
        assert sent == []


def test_quote_is_not_in_the_module_namespace():
    """The import going away is what stops this being reintroduced by a
    later edit reaching for the nearest-looking helper. Checked against the
    namespace, not the source text — the source carries the word in the
    comment explaining why it must not be used."""
    assert not hasattr(mb_enrich, "quote")


class TestArtistIdentityGuard:
    """A high MusicBrainz score is not identity.

    Measured live 2026-08-23: the stage wrote 27 wrong MBIDs, each stamped
    mb_enriched_at so it would never be revisited. Score >= 85 was the only
    condition, and MusicBrainz scores a containing name at 100.
    """

    def _results(self, monkeypatch, names):
        monkeypatch.setattr(
            mb_enrich,
            "_mb_get",
            lambda *a, **k: {
                "artists": [{"id": f"mbid-{i}", "name": n, "score": 100} for i, n in enumerate(names)]
            },
        )

    @pytest.mark.parametrize(
        ("ours", "theirs"),
        [
            ("Red", "Red Hot Chili Peppers"),
            ("Little Feat", "Little Richard"),
            ("Dion", "Celine Dion"),
            ("Jan & Dean", "Jan Arnald"),
            ("Simon & Garfunkel", "Simon Jager"),
            ("Crosby, Stills & Nash", "Bing Crosby"),
            ("Snow", "Hank Snow"),
            ("Dean", "Dean Martin"),
        ],
    )
    def test_a_different_act_is_refused_despite_a_perfect_score(
        self, monkeypatch, ours, theirs
    ):
        self._results(monkeypatch, [theirs])
        assert mb_enrich._search_artist(ours) is None

    @pytest.mark.parametrize(
        ("ours", "theirs"),
        [
            ("R.e.m", "R.E.M."),
            ("Abba", "ABBA"),
            ("a-ha", "a‐ha"),
            ("M.i.a", "M.I.A."),
            ("Beatles, The", "The Beatles"),  # storage form vs natural form
            ("Byrds, The", "The Byrds"),
            ("Morse-Portnoy-George", "Morse Portnoy George"),
        ],
    )
    def test_the_same_act_spelled_differently_is_accepted(self, monkeypatch, ours, theirs):
        self._results(monkeypatch, [theirs])
        hit = mb_enrich._search_artist(ours)
        assert hit is not None
        assert hit[1] == theirs

    def test_a_correct_match_further_down_the_list_still_wins(self, monkeypatch):
        # The wrong-but-high-scoring result must not shadow the right one.
        self._results(monkeypatch, ["Red Hot Chili Peppers", "Red"])
        hit = mb_enrich._search_artist("Red")
        assert hit is not None
        assert hit[1] == "Red"

    def test_empty_name_matches_nothing(self, monkeypatch):
        self._results(monkeypatch, ["Anything"])
        assert mb_enrich._search_artist("") is None
