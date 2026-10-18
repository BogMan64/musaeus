"""Codex audit 2026-10-16, F5: a credential in a request URL (Last.fm's api_key,
AcoustID's client) reached DEBUG logs, exception texts and the network gateway's
record of attempts and denials. None of them may hold the key."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from musaeus import art_sources as A
from musaeus import network_policy as np

SECRET = "AUDIT_DUMMY_LASTFM_CREDENTIAL"
URL = f"https://ws.audioscrobbler.com/2.0/?method=album.getinfo&api_key={SECRET}&artist=A"


@pytest.fixture
def gateway():
    np._gateway.reset()
    yield np._gateway
    np._gateway.reset()


def test_redact_hides_every_credential_parameter():
    url = f"https://x/?api_key={SECRET}&client={SECRET}&token={SECRET}&artist=Keep"
    out = np.redact(url)
    assert SECRET not in out and "artist=Keep" in out and out.count("REDACTED") == 3


def test_a_denied_request_records_and_reports_no_key(gateway):
    with np.policy(np.NetworkPolicy.LOCAL_ONLY), pytest.raises(A.ArtUnavailable) as err:
        A._get(URL)
    assert gateway.denials, "no denial was recorded"
    assert SECRET not in str(err.value)
    assert all(SECRET not in t for t in gateway.attempts + gateway.denials)


def test_an_http_failure_and_bad_json_name_no_key(gateway, monkeypatch, caplog):
    def http_503(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b""))

    with np.policy(np.NetworkPolicy.ALLOWED):
        monkeypatch.setattr(A.urllib.request, "urlopen", http_503)
        with caplog.at_level("DEBUG"), pytest.raises(A.ArtUnavailable) as err:
            A._get(URL)
        assert SECRET not in str(err.value)
        monkeypatch.setattr(A, "_get", lambda url, accept=None: b"not json")
        with pytest.raises(A.ArtUnavailable) as err2:
            A._get_json(URL)
        assert SECRET not in str(err2.value)
    assert SECRET not in caplog.text
    assert all(SECRET not in t for t in gateway.attempts)
    assert json.dumps(gateway.attempts)  # still a usable record: service and endpoint kept
    assert any("ws.audioscrobbler.com" in t for t in gateway.attempts)


def test_a_transport_error_carrying_the_url_names_no_key(gateway, monkeypatch, caplog):
    # Codex review of #207: some URLError texts embed the URL itself.
    def down(req, timeout=None):
        raise urllib.error.URLError(f"cannot reach {req.full_url}")

    with np.policy(np.NetworkPolicy.ALLOWED):
        monkeypatch.setattr(A.urllib.request, "urlopen", down)
        with caplog.at_level("DEBUG"), pytest.raises(A.ArtUnavailable) as err:
            A._get(URL)
    assert SECRET not in str(err.value) and err.value.__cause__ is None
    assert SECRET not in caplog.text


def test_the_album_art_debug_log_names_no_key(gateway, monkeypatch, caplog):
    # Review of #207: the DEBUG line that actually prints a source's failure is
    # fetch_album_art's. Driven end to end with only Last.fm able to answer.
    def down(req, timeout=None):
        raise urllib.error.URLError(f"cannot reach {req.full_url}")

    for name in ("fetch_itunes", "fetch_deezer", "fetch_coverartarchive"):
        monkeypatch.setattr(A, name, lambda *a, **k: None)
    with np.policy(np.NetworkPolicy.ALLOWED):
        monkeypatch.setattr(A.urllib.request, "urlopen", down)
        with caplog.at_level("DEBUG"):
            A.fetch_album_art("Artist", "Album", lastfm_key=SECRET)
    assert "lastfm" in caplog.text and SECRET not in caplog.text


@pytest.mark.parametrize("form", ["api-key", "client_id", "api_sig", "API_KEY"])
def test_other_spellings_of_a_key_are_hidden_too(form):
    assert SECRET not in np.redact(f"https://x/?a=1&{form}={SECRET}&b=2")
