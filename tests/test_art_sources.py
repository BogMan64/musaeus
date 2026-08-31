"""Network album-art sources, and the quality bar they answer to.

AlbumArtStage could only embed a sidecar already on disk -- zero network
calls -- so a file whose folder had no cover.jpg stayed without art for
ever. An earlier note in this project concluded art was blocked on
`mb_release` being empty; that was wrong twice over. iTunes and Last.fm
need no MBID at all, and the Cover Art Archive path works from a
RELEASE-GROUP mbid, which is derived by searching MusicBrainz.
"""

from __future__ import annotations

import io
import struct

import pytest

from musaeus import art_sources as A
from musaeus.art_quality import MIN_EDGE_PX, describe, image_dimensions, is_too_small


def _png(w: int, h: int, pad: int = 20_000) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", w, h) + b"\x00" * pad


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


# ── dimensions ────────────────────────────────────────────────────────────────


def test_png_dimensions_are_read_from_the_header():
    assert image_dimensions(_png(600, 400)) == (600, 400)


def test_an_unrecognised_blob_has_no_dimensions():
    assert image_dimensions(b"not an image at all") is None
    assert image_dimensions(b"") is None


def test_too_small_uses_the_longest_edge():
    assert is_too_small(_png(300, 300))
    assert not is_too_small(_png(MIN_EDGE_PX, 100)), "600x100 is wide enough"


def test_unreadable_dimensions_are_not_reported_as_too_small():
    """"Cannot tell" is not "too small" -- flagging on ignorance would send
    every unusual image for replacement."""
    assert not is_too_small(b"\xff\xd8\xff" + b"\x00" * 30_000)


def test_describe_flags_the_small_ones():
    assert "TOO SMALL" in describe(_png(220, 220))
    assert "TOO SMALL" not in describe(_png(1000, 1000))


# ── source selection ──────────────────────────────────────────────────────────


class TestSourcePreference:
    def test_a_usable_image_short_circuits_the_remaining_sources(self, monkeypatch):
        calls = []
        monkeypatch.setattr(A, "fetch_itunes",
                            lambda a, b: (calls.append("itunes"), _png(600, 600))[1])
        monkeypatch.setattr(A, "fetch_coverartarchive",
                            lambda a, b: calls.append("caa"))
        monkeypatch.setattr(A, "fetch_lastfm",
                            lambda a, b, k: calls.append("lastfm"))
        blob, src = A.fetch_album_art("Artist", "Album")
        assert src == "itunes"
        assert calls == ["itunes"], "kept asking after a good answer"

    def test_undersized_art_does_not_stop_the_search(self, monkeypatch):
        """A 300x300 from the first source is worse than a 600x600 from the
        second; embedding the small one only moves the file from 'no art' to
        'bad art'."""
        monkeypatch.setattr(A, "fetch_itunes", lambda a, b: _png(300, 300))
        monkeypatch.setattr(A, "fetch_coverartarchive", lambda a, b: _png(1000, 1000))
        monkeypatch.setattr(A, "fetch_lastfm", lambda a, b, k: None)
        blob, src = A.fetch_album_art("Artist", "Album")
        assert src == "coverartarchive"
        assert image_dimensions(blob) == (1000, 1000)

    def test_undersized_art_is_still_returned_when_it_is_all_there_is(self, monkeypatch):
        monkeypatch.setattr(A, "fetch_itunes", lambda a, b: _png(300, 300))
        monkeypatch.setattr(A, "fetch_coverartarchive", lambda a, b: None)
        monkeypatch.setattr(A, "fetch_lastfm", lambda a, b, k: None)
        blob, src = A.fetch_album_art("Artist", "Album")
        assert image_dimensions(blob) == (300, 300), "some art beats no art"

    def test_no_art_anywhere_is_none_not_an_error(self, monkeypatch):
        for n in ("fetch_itunes", "fetch_coverartarchive"):
            monkeypatch.setattr(A, n, lambda *a: None)
        monkeypatch.setattr(A, "fetch_lastfm", lambda *a: None)
        assert A.fetch_album_art("Artist", "Album") is None

    def test_every_source_failing_raises_rather_than_reporting_no_art(self, monkeypatch):
        """Three states, not two. A timeout must not settle a row as
        'this album has no art' -- the same rule as mb_enrich's
        LookupUnavailable."""
        def boom(*a):
            raise A.ArtUnavailable("timeout")
        for n in ("fetch_itunes", "fetch_coverartarchive", "fetch_lastfm"):
            monkeypatch.setattr(A, n, boom)
        with pytest.raises(A.ArtUnavailable):
            A.fetch_album_art("Artist", "Album")


# ── the network gateway is respected ──────────────────────────────────────────


def test_no_source_reaches_the_network_under_local_only(monkeypatch):
    """Preview must not fetch. Same gateway Enrich and MBEnrich answer to."""
    from musaeus.network_policy import NetworkPolicy, policy

    monkeypatch.setattr(A.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("reached the network"))
    with policy(NetworkPolicy.LOCAL_ONLY):
        # No art, and -- the point -- no urlopen. With a Last.fm key every
        # source is refused, so the three-state rule applies and it raises;
        # without one Last.fm is SKIPPED rather than refused, which is not
        # the same thing, and the call simply yields nothing.
        with pytest.raises(A.ArtUnavailable):
            A.fetch_album_art("Artist", "Album", lastfm_key="a-key")
        assert A.fetch_album_art("Artist", "Album") is None


def test_an_error_page_is_not_mistaken_for_an_image(monkeypatch):
    """Checked on magic bytes, because several of these services return a
    200 with an HTML body and an image Content-Type."""
    assert not A._looks_like_image(b"<html>404</html>" * 900)
    assert not A._looks_like_image(_png(600, 600, pad=10)), "too small to be real"
    assert A._looks_like_image(_png(600, 600))


def test_itunes_prefers_an_exact_album_match(monkeypatch):
    seen = {}

    def fake_json(url):
        return {"results": [
            {"collectionName": "Wrong Album", "artworkUrl100": "http://x/wrong100x100bb.png"},
            {"collectionName": "Right Album", "artworkUrl100": "http://x/right100x100bb.png"},
        ]}

    def fake_get(url, accept=None):
        seen["url"] = url
        return _png(600, 600)

    monkeypatch.setattr(A, "_get_json", fake_json)
    monkeypatch.setattr(A, "_get", fake_get)
    A.fetch_itunes("Artist", "Right Album")
    assert "right" in seen["url"], f"took the wrong album: {seen['url']}"
    assert "600x600" in seen["url"], "did not upgrade the thumbnail size"
