"""Discogs is a secondary attempt when MusicBrainz has no confident match.

Same three-state contract as mb_enrich, and for the same reason: a
transport failure ("no answer") must never be recorded the same way as a
genuine miss ("asked, Discogs has no such artist"), because a genuine
miss is meant to be permanent and a transport failure is meant to be
retried. Conflating them is exactly the bug test_mb_enrich_no_answer.py
exists to pin for MusicBrainz -- mirrored here before it has the chance
to recur in a second module.

Auth is a consumer key/secret pair sent as query parameters ("app auth"),
not the personal-token header this module started with. A personal token
was tried first and rejected live 2026-09-03 with HTTP 401 "Invalid
consumer token" despite matching ORPHEUS's own previously-working header
format exactly; the key/secret pair Grey supplied afterward was verified
live against the real /database/search endpoint before any test here was
written against it.
"""

from __future__ import annotations

import pytest

from musaeus import discogs
from musaeus.discogs import DiscogsAuthError, LookupUnavailable, search_artist

_KEY = "fake-consumer-key"
_SECRET = "fake-consumer-secret"


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
        def fake(path, params, key, secret):
            raise boom

        monkeypatch.setattr(discogs, "_discogs_get", fake)
        with pytest.raises(LookupUnavailable):
            search_artist("Jake Shimabukuro", _KEY, _SECRET)

    def test_a_real_miss_returns_none(self, monkeypatch):
        monkeypatch.setattr(discogs, "_discogs_get", lambda p, params, k, s: {"results": []})
        assert search_artist("Not A Real Band At All", _KEY, _SECRET) is None

    def test_a_hit_returns_the_pair(self, monkeypatch):
        monkeypatch.setattr(
            discogs,
            "_discogs_get",
            lambda p, params, k, s: {"results": [{"id": 123456, "title": "Jake Shimabukuro"}]},
        )
        assert search_artist("Jake Shimabukuro", _KEY, _SECRET) == ("123456", "Jake Shimabukuro")


class TestExactMatchOnly:
    def test_a_containing_name_is_not_accepted(self, monkeypatch):
        monkeypatch.setattr(
            discogs,
            "_discogs_get",
            lambda p, params, k, s: {
                "results": [{"id": 1, "title": "The Jake Shimabukuro Tribute Band"}]
            },
        )
        assert search_artist("Jake Shimabukuro", _KEY, _SECRET) is None

    def test_case_insensitive_exact_match_is_accepted(self, monkeypatch):
        monkeypatch.setattr(
            discogs,
            "_discogs_get",
            lambda p, params, k, s: {"results": [{"id": 1, "title": "jake shimabukuro"}]},
        )
        assert search_artist("Jake Shimabukuro", _KEY, _SECRET) == ("1", "jake shimabukuro")

    def test_the_first_exact_match_wins_when_several_are_present(self, monkeypatch):
        monkeypatch.setattr(
            discogs,
            "_discogs_get",
            lambda p, params, k, s: {
                "results": [
                    {"id": 1, "title": "Some Other Band"},
                    {"id": 2, "title": "Jake Shimabukuro"},
                    {"id": 3, "title": "Jake Shimabukuro"},
                ]
            },
        )
        assert search_artist("Jake Shimabukuro", _KEY, _SECRET) == ("2", "Jake Shimabukuro")


class TestAuthFailureIsStillLookupUnavailable:
    @pytest.mark.parametrize("code", [401, 403])
    def test_a_rejected_credential_raises_discogs_auth_error(self, monkeypatch, code):
        import urllib.error

        def fake_urlopen(req, timeout):
            raise urllib.error.HTTPError(req.full_url, code, "Unauthorized", {}, None)

        monkeypatch.setattr(discogs, "urlopen", fake_urlopen)
        monkeypatch.setattr(discogs, "_network_check", lambda base: None)
        with pytest.raises(DiscogsAuthError):
            search_artist("Jake Shimabukuro", "bad-key", "bad-secret")

    def test_discogs_auth_error_is_a_lookup_unavailable(self):
        assert issubclass(DiscogsAuthError, LookupUnavailable)


class TestRequestShape:
    def test_key_and_secret_are_sent_as_query_parameters(self, monkeypatch):
        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            raise TimeoutError("stop here, we only need the request shape")

        monkeypatch.setattr(discogs, "urlopen", fake_urlopen)
        monkeypatch.setattr(discogs, "_network_check", lambda base: None)
        with pytest.raises(LookupUnavailable):
            search_artist("Test Artist", "my-key-123", "my-secret-456")

        assert "key=my-key-123" in captured["url"]
        assert "secret=my-secret-456" in captured["url"]
        assert "database/search" in captured["url"]
        assert "type=artist" in captured["url"]

    def test_no_oauth_authorization_header_is_sent(self, monkeypatch):
        captured = {}

        def fake_urlopen(req, timeout):
            captured["headers"] = dict(req.header_items())
            raise TimeoutError("stop here, we only need the request shape")

        monkeypatch.setattr(discogs, "urlopen", fake_urlopen)
        monkeypatch.setattr(discogs, "_network_check", lambda base: None)
        with pytest.raises(LookupUnavailable):
            search_artist("Test Artist", "my-key-123", "my-secret-456")

        assert "Authorization" not in captured["headers"]
