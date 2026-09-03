#!/usr/bin/env python3
"""
MUSAEUS — network sources for missing album art.

Why this exists
---------------
`AlbumArtStage` could only embed a sidecar image that was already on disk --
zero network calls -- so a file whose folder had no `cover.jpg` stayed
without art for ever. Measured 2026-08-31: 7 files in ALAC-Library had none,
and the stage had no way to get any.

Ported from ORPHEUS's SCRIPTS/orpheus_art_forger.py, which had solved this
already. Three sources, tried in order:

  iTunes Search      artist + album text. No key, no MBID.
  Cover Art Archive  via the RELEASE-GROUP mbid, which is derived here by
                     searching MusicBrainz for the artist. NOT the release
                     mbid -- an earlier note in this project wrongly
                     concluded art was blocked on `mb_release` being empty.
  Last.fm            artist + album text. Needs LASTFM_API_KEY.

Every request goes through `network_policy.check` first, so a preview or an
unattended run cannot reach the network by accident -- the same gateway
Enrich/MBEnrich answer to.

Nothing here writes a file. It returns bytes and lets the caller decide,
because embedding is a mutation and belongs behind the stage's dry-run.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

from .art_quality import MIN_EDGE_PX, describe, image_dimensions, is_too_small
from .network_policy import check as _network_check

logger = logging.getLogger(__name__)

_UA = "Musaeus/1.0 (music-library-manager; contact@musaeus.local)"
_TIMEOUT_S = 15

# Below this an image is not worth embedding -- see art_quality.MIN_EDGE_PX.
_MIN_BYTES = 8_000

# MusicBrainz asks for <=1 req/s from anonymous clients. The other two are
# not rate limited but a courtesy pause costs nothing at this volume.
_MB_RATE_S = 1.05


class ArtUnavailable(Exception):
    """No answer was obtained. Distinct from "no art exists for this album".

    The same three-state discipline as mb_enrich's LookupUnavailable: a
    timeout must not be recorded as "this album has no art", or the row is
    settled on a network wobble and never asked again.
    """


def _get(url: str, *, accept: str | None = None) -> bytes | None:
    """Fetch bytes. None when the server answered 404/no-content."""
    # Inside the failure path, not beside it. A policy refusal is "no answer
    # obtained", exactly as mb_enrich treats it -- so a preview degrades to
    # "could not ask" rather than crashing the stage.
    try:
        _network_check(url)
    except Exception as exc:
        raise ArtUnavailable(f"refused by network policy: {exc}") from exc

    headers = {"User-Agent": _UA}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 400):
            return None          # answered: nothing there
        raise ArtUnavailable(f"HTTP {exc.code} for {url}") from exc
    except Exception as exc:
        raise ArtUnavailable(str(exc)) from exc


def _get_json(url: str) -> dict | None:
    raw = _get(url, accept="application/json")
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ArtUnavailable(f"unparseable JSON from {url}: {exc}") from exc


def _looks_like_image(blob: bytes | None) -> bool:
    """A real image, not an error page. Checked on magic bytes, not on
    Content-Type, which several of these services get wrong."""
    if not blob or len(blob) < _MIN_BYTES:
        return False
    return blob[:3] == b"\xff\xd8\xff" or blob[:8] == b"\x89PNG\r\n\x1a\n"


# ── sources ───────────────────────────────────────────────────────────────────


def fetch_itunes(artist: str, album: str) -> bytes | None:
    """iTunes Search. No key and no MBID needed, so it is tried first."""
    if not artist:
        return None
    term = f"{artist} {album}".strip()
    url = "https://itunes.apple.com/search?" + urlencode(
        {"term": term, "media": "music", "entity": "album", "limit": 5}
    )
    data = _get_json(url)
    if not data:
        return None
    want = _norm(album)
    results = data.get("results", [])
    # Prefer an exact album match; fall back to the first result only when
    # no album was given to match against.
    ordered = [r for r in results if want and _norm(r.get("collectionName", "")) == want]
    if not ordered:
        ordered = results if not want else []
    for r in ordered:
        art = r.get("artworkUrl100")
        if not art:
            continue
        # iTunes serves any size by substitution. Measured 2026-08-31: it
        # honours 1200x1200 and caps around 2400x2400. 600 was only just over
        # the 500px floor, so a replacement pass had almost nothing to gain;
        # 1200 leaves real headroom and still costs ~400KB rather than ~950KB.
        blob = _get(art.replace("100x100bb", "1200x1200bb"))
        if _looks_like_image(blob):
            return blob
    return None


def fetch_deezer(artist: str, album: str) -> bytes | None:
    """Deezer search. Keyless like iTunes, and `cover_xl` is 1000x1000.

    Added 2026-08-31 when Grey asked about Amazon as a source. Amazon has no
    open cover endpoint -- its Product Advertising API needs an Associates
    account with qualifying sales -- so this fills the same gap: a second
    keyless, high-resolution source that answers when iTunes does not.
    """
    if not artist:
        return None
    term = f"{artist} {album}".strip()
    data = _get_json("https://api.deezer.com/search/album?" + urlencode(
        {"q": term, "limit": 5}
    ))
    if not data:
        return None
    want = _norm(album)
    results = data.get("data", []) or []
    ordered = [r for r in results if want and _norm(r.get("title", "")) == want]
    if not ordered:
        ordered = results if not want else []
    for r in ordered:
        for key in ("cover_xl", "cover_big"):
            u = r.get(key)
            if not u:
                continue
            blob = _get(u)
            if _looks_like_image(blob):
                return blob
    return None


def fetch_coverartarchive(artist: str, album: str) -> bytes | None:
    """Cover Art Archive, via the RELEASE-GROUP mbid.

    The release-group is found by searching MusicBrainz for artist + album,
    so this needs no pre-existing MBID in our own database.
    """
    if not artist or not album:
        return None
    q = quote(f'artist:"{artist}" AND releasegroup:"{album}"')
    data = _get_json(f"https://musicbrainz.org/ws/2/release-group?query={q}&limit=3&fmt=json")
    time.sleep(_MB_RATE_S)
    if not data:
        return None
    for rg in data.get("release-groups", []):
        rgid = rg.get("id")
        if not rgid:
            continue
        blob = _get(f"https://coverartarchive.org/release-group/{rgid}/front")
        if _looks_like_image(blob):
            return blob
    return None


def fetch_lastfm(artist: str, album: str, api_key: str) -> bytes | None:
    """Last.fm album.getinfo. Needs a key; skipped silently without one."""
    if not (api_key and artist and album):
        return None
    url = "https://ws.audioscrobbler.com/2.0/?" + urlencode(
        {"method": "album.getinfo", "api_key": api_key, "artist": artist,
         "album": album, "format": "json"}
    )
    data = _get_json(url)
    if not data:
        return None
    images = (data.get("album") or {}).get("image", [])
    # Largest first -- the list is ordered small..extralarge/mega.
    for img in reversed(images):
        u = img.get("#text")
        if not u:
            continue
        blob = _get(u)
        if _looks_like_image(blob):
            return blob
    return None


def _longest_edge(blob: bytes) -> int:
    dims = image_dimensions(blob)
    return max(dims) if dims else 0


def _norm(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())


def fetch_album_art(
    artist: str, album: str, lastfm_key: str = "", min_edge: int = MIN_EDGE_PX
) -> tuple[bytes, str] | None:
    """First usable image from any source, with the source that supplied it.

    `min_edge` is the longest-edge floor an image must clear to be returned
    outright. It exists so a REPLACEMENT can ask for something better than
    what the file already carries: passing the current art's longest edge
    means a source offering the same small image again is not accepted as an
    improvement. Defaults to the library-wide MIN_EDGE_PX.

    Returns None when every source ANSWERED and none had art. Raises
    ArtUnavailable only when nothing answered at all, so a caller can tell
    "this album has no art" from "we could not ask".
    """
    attempts = 0
    unavailable = 0
    best: tuple[bytes, str] | None = None   # largest sub-threshold fallback
    for name, fn in (
        ("itunes", lambda: fetch_itunes(artist, album)),
        ("deezer", lambda: fetch_deezer(artist, album)),
        ("coverartarchive", lambda: fetch_coverartarchive(artist, album)),
        ("lastfm", lambda: fetch_lastfm(artist, album, lastfm_key)),
    ):
        attempts += 1
        try:
            blob = fn()
        except ArtUnavailable as exc:
            unavailable += 1
            logger.debug("[art] %s unavailable for %r/%r: %s", name, artist, album, exc)
            continue
        if not blob:
            continue
        # Do not settle for art the quality check would immediately flag.
        # Keep the best seen, but keep looking -- a 300x300 from the first
        # source is worse than a 600x600 from the second, and embedding the
        # small one just moves the file from "no art" to "bad art".
        if is_too_small(blob, min_edge):
            logger.debug("[art] %s gave %s for %r / %r -- holding, trying next",
                         name, describe(blob), artist, album)
            # Keep the LARGEST sub-threshold image, not the first one seen:
            # if nothing clears the floor we still want the best on offer.
            if best is None or _longest_edge(blob) > _longest_edge(best[0]):
                best = (blob, name)
            continue
        logger.info("[art] %s supplied %s for %r / %r", name, describe(blob), artist, album)
        return blob, name

    if best is not None:
        logger.info("[art] only undersized art found for %r / %r: %s from %s",
                    artist, album, describe(best[0]), best[1])
        return best
    if unavailable == attempts:
        raise ArtUnavailable("no art source answered")
    return None
