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
