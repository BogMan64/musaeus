"""Discogs is a secondary attempt when MusicBrainz has no confident match.

Same three-state contract as mb_enrich, and for the same reason: a
transport failure ("no answer") must never be recorded the same way as a
genuine miss ("asked, Discogs has no such artist"), because a genuine
miss is meant to be permanent and a transport failure is meant to be
retried. Conflating them is exactly the bug test_mb_enrich_no_answer.py
exists to pin for MusicBrainz -- mirrored here before it has the chance
to recur in a second module.
"""

from __future__ import annotations

import pytest

from musaeus import discogs
from musaeus.discogs import DiscogsAuthError, LookupUnavailable, search_artist


class TestTheThreeStates:
    @pytest.mark.parametrize(
        "boom",
        [
            TimeoutError("The read operation timed out"),
            OSError("HTTP Error 503: Service Temporarily Unavailable"),
            ConnectionError("dns failure"),
        ],
    )
    def test_no_answer_raises_lookup_unavailable(self, monkeypatch, boom):
        def fake(path, params, api_key):
            raise boom

        monkeypatch.setattr(discogs, "_discogs_get", fake)
        with pytest.raises(LookupUnavailable):
            search_artist("Jake Shimabukuro", "fake-key")

    def test_a_real_miss_returns_none(self, monkeypatch):
        # Discogs answered; it simply has nothing matching. That IS an
        # answer and must stay distinguishable from no answer at all.
        monkeypatch.setattr(discogs, "_discogs_get", lambda p, params, k: {"results": []})
        assert search_artist("Not A Real Band At All", "fake-key") is None

    def test_a_hit_returns_the_pair(self, monkeypatch):
        monkeypatch.setattr(
            discogs,
            "_discogs_get",
            lambda p, params, k: {"results": [{"id": 123456, "title": "Jake Shimabukuro"}]},
        )
        assert search_artist("Jake Shimabukuro", "fake-key") == ("123456", "Jake Shimabukuro")


class TestExactMatchOnly:
    """Discogs search results carry no confidence score comparable to
    MusicBrainz's -- mb_enrich._same_artist exists because "score 100"
    can still mean the wrong artist ("Red" -> Red Hot Chili Peppers).
    Without a score to lean on here, matching is exact-name only."""

    def test_a_containing_name_is_not_accepted(self, monkeypatch):
        monkeypatch.setattr(
            discogs,
            "_discogs_get",
            lambda p, params, k: {
                "results": [{"id": 1, "title": "The Jake Shimabukuro Tribute Band"}]
            },
        )
        assert search_artist("Jake Shimabukuro", "fake-key") is None

    def test_case_insensitive_exact_match_is_accepted(self, monkeypatch):
        monkeypatch.setattr(
            discogs,
            "_discogs_get",
            lambda p, params, k: {"results": [{"id": 1, "title": "jake shimabukuro"}]},
        )
        assert search_artist("Jake Shimabukuro", "fake-key") == ("1", "jake shimabukuro")

    def test_the_first_exact_match_wins_when_several_are_present(self, monkeypatch):
        monkeypatch.setattr(
            discogs,
            "_discogs_get",
            lambda p, params, k: {
                "results": [
                    {"id": 1, "title": "Some Other Band"},
                    {"id": 2, "title": "Jake Shimabukuro"},
                    {"id": 3, "title": "Jake Shimabukuro"},
                ]
            },
        )
        assert search_artist("Jake Shimabukuro", "fake-key") == ("2", "Jake Shimabukuro")


class TestAuthFailureIsStillLookupUnavailable:
    """A bad/expired key must not be cached as 'artist not found' -- that
    would silently mark the whole library as Discogs-checked-and-missing
    because of a credential problem, not an artist problem. Verified
    against the real API 2026-09-03: an invalid token returns HTTP 401
    with body {"message": "Invalid consumer token. Please register an
    app before making requests."}."""

    def test_a_401_raises_discogs_auth_error(self, monkeypatch):
        import urllib.error

        def fake_urlopen(req, timeout):
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", {}, None
            )

        monkeypatch.setattr(discogs, "urlopen", fake_urlopen)
        monkeypatch.setattr(discogs, "_network_check", lambda base: None)
        with pytest.raises(DiscogsAuthError):
            search_artist("Jake Shimabukuro", "bad-key")

    def test_discogs_auth_error_is_a_lookup_unavailable(self):
        """So a caller that only catches LookupUnavailable (the common
        case) still handles an auth failure correctly without needing to
        know about the more specific subclass."""
        assert issubclass(DiscogsAuthError, LookupUnavailable)


class TestRequestShape:
    """Verified live against the real Discogs API 2026-09-03 (with the
    key on file, which the API rejected as invalid -- see
    DiscogsAuthError's docstring) that this exact header format is what
    ORPHEUS's own orpheus_genre_classify.py already used successfully,
    confirming the auth MECHANICS are right independent of key validity.
    """

    def test_the_authorization_header_matches_discogs_documented_format(self, monkeypatch):
        captured = {}

        def fake_urlopen(req, timeout):
            captured["headers"] = dict(req.header_items())
            captured["url"] = req.full_url
            raise TimeoutError("stop here, we only need the request shape")

        monkeypatch.setattr(discogs, "urlopen", fake_urlopen)
        monkeypatch.setattr(discogs, "_network_check", lambda base: None)
        with pytest.raises(LookupUnavailable):
            search_artist("Test Artist", "my-token-123")

        assert captured["headers"]["Authorization"] == "Discogs token=my-token-123"
        assert "database/search" in captured["url"]
        assert "type=artist" in captured["url"]
