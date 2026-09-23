"""Album lookups must ask in the natural form, and cache under it.

This is the defect that justified moving the script into the repository.

MUSAEUS stores the article as a suffix -- "Beatles, The" -- so a
folder-browsed library sorts under B. No music service has heard of that
string. `musaeus/artist_form.py` had already measured this on 2026-08-29
(376 of 839 cached misses in `X, The` form; 0 of 2,158 hits), but the
proposal script lived on the Desktop, could not import that module, and
queried the stored form.

Measured 2026-09-14 on the 6,142-row proposal CSV: 1,072 rows carried a
trailing-article artist and **1,071 of them came back "4-NO ANSWER"** --
99.9% -- against 0% of the rows where the sources merely disagreed. Re-asking
25 of them in the natural form resolved 19, five of those to 1-AGREED.

These tests pin the conversion and the cache key. They use no network.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "album_names"))

from propose_album_names import (  # noqa: E402
    Answer,
    Throttle,
    cache_get,
    cache_open,
    cache_put,
    natural_form,
)


class TestTheFormWeAskIn:
    @pytest.mark.parametrize(
        "stored,asked",
        [
            ("Beatles, The", "The Beatles"),
            ("Who, The", "The Who"),
            ("Chieftains, The", "The Chieftains"),
            ("5th Dimension, The", "The 5th Dimension"),
            ("Ad Libs, The", "The Ad Libs"),
        ],
    )
    def test_a_trailing_article_is_moved_to_the_front(self, stored, asked):
        assert natural_form(stored) == asked

    @pytest.mark.parametrize("name", ["De La Soul", "Los Lobos", "Fleetwood Mac", "ABBA"])
    def test_a_name_that_merely_contains_an_article_word_is_untouched(self, name):
        """PROTECTED_ARTIST_NAMES exists because this rule has regressed
        three times, once splitting De La Soul into "La Soul, De"."""
        assert natural_form(name) == name

    def test_the_conversion_is_not_applied_twice(self):
        assert natural_form(natural_form("Beatles, The")) == "The Beatles"


class TestTheCacheKey:
    """The two spellings are one artist and must share one cache row.

    Keyed on the stored form, "Beatles, The" and "The Beatles" are separate
    rows: the lookup gets made twice and answered once, and an answer found
    under one spelling is invisible to the other.
    """

    @pytest.fixture
    def cache(self, tmp_path) -> sqlite3.Connection:
        return cache_open(tmp_path / "cache.db")

    def test_an_answer_stored_naturally_is_found_from_the_stored_form(self, cache):
        cache_put(cache, "The Who", "Baba O'Riley", "itunes", Answer(album="Who's Next"))

        # what the caller does: convert first, then look up
        hit = cache_get(cache, natural_form("Who, The"), "Baba O'Riley", "itunes")
        assert hit is not None
        assert hit.album == "Who's Next"

    def test_the_stored_form_alone_does_not_find_it(self, cache):
        """Guards the regression directly: if a future edit drops the
        natural_form() call at the lookup site, this is what happens."""
        cache_put(cache, "The Who", "Baba O'Riley", "itunes", Answer(album="Who's Next"))
        assert cache_get(cache, "Who, The", "Baba O'Riley", "itunes") is None


class TestMusicBrainzPacing:
    """MusicBrainz enforces ~1 req/s for anonymous clients and 503s if pushed.

    --rate is the operator's choice for iTunes and Deezer; it must not be
    allowed to drive MusicBrainz past what MusicBrainz permits.
    """

    def test_a_fast_rate_does_not_apply_to_musicbrainz(self):
        t = Throttle(0.2)
        assert t.gap_for("itunes") == pytest.approx(0.2)
        assert t.gap_for("musicbrainz") >= 1.0

    def test_a_slow_rate_is_respected_for_every_source(self):
        t = Throttle(3.0)
        assert t.gap_for("itunes") == pytest.approx(3.0)
        assert t.gap_for("musicbrainz") == pytest.approx(3.0)


class TestMusicBrainzUsesTheArticleInsensitiveKey:
    """The article is not the same on every source.

    natural_form() rescued iTunes and Deezer -- 1,071 rows that had returned
    nothing. MusicBrainz is the other way for SOME artists: it canonicalises
    without the leading "The" and its quoted phrase search is exact, so
    adding the article returns nothing at all. Measured 2026-09-15:

        artist:"Traveling Wilburys"      -> 3 recordings
        artist:"The Traveling Wilburys"  -> 0

    Two fixes were needed and the first alone did nothing. Retrying the
    SEARCH without the article found the recordings; the artist-credit
    FILTER then threw them all away, because fold("The Traveling Wilburys")
    is not fold("Traveling Wilburys"). comparison_key is the codebase's own
    answer -- every form of one name compares equal -- and it protects
    "De La Soul" and "Peter, Paul and Mary" from being mangled, which a
    fresh regex here would not.
    """

    @pytest.mark.parametrize(
        "ours,theirs",
        [
            ("The Traveling Wilburys", "Traveling Wilburys"),
            ("The Beatles", "Beatles, The"),
            ("Beatles, The", "The Beatles"),
        ],
    )
    def test_every_form_of_a_name_compares_equal(self, ours, theirs):
        from musaeus.artist_form import comparison_key

        assert comparison_key(ours) == comparison_key(theirs)

    @pytest.mark.parametrize("name", ["De La Soul", "Los Lobos", "Peter, Paul and Mary"])
    def test_a_name_that_is_not_an_article_form_is_not_mangled(self, name):
        """The reason to reuse comparison_key instead of a local regex:
        tribute_quarantine grew its own and turned "Healing, The" into
        "healing", which then missed the protected entry "the healing"."""
        from musaeus.artist_form import comparison_key

        assert comparison_key(name) == name.lower()

    def test_the_filter_uses_comparison_key_not_an_exact_fold(self):
        """Guards the half that was missed: the retry search succeeded and
        the filter discarded its results anyway."""
        import inspect

        import propose_album_names as m

        src = inspect.getsource(m.ask_musicbrainz)
        assert "comparison_key" in src
        assert "fold((c.get" not in src, "the artist credit must not be matched by exact fold"


class TestTheArticleFallbackAppliesToEverySource:
    """Neither form of a name is right for every artist.

    natural_form() turns "Eagles, The" into "The Eagles" and rescued 1,071
    rows that every source had refused. For some bands that same fix is the
    bug, because they are canonically credited WITHOUT the article:

        measured 2026-09-15
        "The Eagles" + "Hotel California" -> iTunes '', Deezer ''
        "Eagles"     + "Hotel California" -> iTunes and Deezer both answer
        "The Traveling Wilburys" -> MusicBrainz 0 recordings
        "Traveling Wilburys"     -> MusicBrainz 3

    No table says which artists are which, so the only honest move is to ask
    again. One wrapper for all three sources -- this codebase has been bitten
    repeatedly by one rule living in several modules and drifting.
    """

    def test_a_hit_on_the_first_try_costs_one_call(self):
        from propose_album_names import Answer, Throttle, ask_with_article_fallback

        calls = []

        def fake(artist, title, throttle):
            calls.append(artist)
            return Answer(album="Hotel California", source="itunes")

        got = ask_with_article_fallback(fake, "The Eagles", "x", Throttle(0))
        assert got.album == "Hotel California"
        assert calls == ["The Eagles"], "a source that answered must not be asked twice"

    def test_a_miss_retries_without_the_article(self):
        from propose_album_names import Answer, Throttle, ask_with_article_fallback

        calls = []

        def fake(artist, title, throttle):
            calls.append(artist)
            return Answer(album="Desperado") if artist == "Eagles" else Answer()

        got = ask_with_article_fallback(fake, "The Eagles", "x", Throttle(0))
        assert got.album == "Desperado"
        assert calls == ["The Eagles", "Eagles"]

    def test_an_artist_without_an_article_is_never_retried(self):
        """The retry doubles the request count against a 1/second budget, so
        it must fire only where it could possibly help."""
        from propose_album_names import Answer, Throttle, ask_with_article_fallback

        calls = []

        def fake(artist, title, throttle):
            calls.append(artist)
            return Answer()

        ask_with_article_fallback(fake, "Fleetwood Mac", "x", Throttle(0))
        assert calls == ["Fleetwood Mac"]

    def test_both_misses_return_an_empty_answer_not_a_crash(self):
        from propose_album_names import Answer, Throttle, ask_with_article_fallback

        got = ask_with_article_fallback(lambda a, t, th: Answer(), "The Nobodies", "x", Throttle(0))
        assert got.album == ""

    def test_the_loop_actually_uses_the_wrapper(self):
        """A unit test on the wrapper cannot catch the caller bypassing it."""
        import inspect

        import propose_album_names as m

        assert "ask_with_fallbacks(" in inspect.getsource(m.main), (
            "the loop must go through the wrapper that applies BOTH the article "
            "and the title fallback, not the article one directly"
        )
