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
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode

from .art_quality import MIN_EDGE_PX, describe, image_dimensions, is_too_small
from .artist_form import act_key, folder_artist
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
            body: bytes = resp.read()
            return body
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 400):
            return None  # answered: nothing there
        raise ArtUnavailable(f"HTTP {exc.code} for {url}") from exc
    except Exception as exc:
        raise ArtUnavailable(str(exc)) from exc


def _get_json(url: str) -> dict | None:
    raw = _get(url, accept="application/json")
    if raw is None:
        return None
    try:
        parsed: dict = json.loads(raw.decode("utf-8"))
        return parsed
    except Exception as exc:
        raise ArtUnavailable(f"unparseable JSON from {url}: {exc}") from exc


def _looks_like_image(blob: bytes | None) -> bool:
    """A real image, not an error page. Checked on magic bytes, not on
    Content-Type, which several of these services get wrong."""
    if not blob or len(blob) < _MIN_BYTES:
        return False
    return blob[:3] == b"\xff\xd8\xff" or blob[:8] == b"\x89PNG\r\n\x1a\n"


# ── whose album is it ─────────────────────────────────────────────────────────
#
# A search ranks; it does not say whose album a result is. Asked for one
# act's "Greatest Hits", iTunes and Deezer can answer with another act's
# first, and the title check alone took it (Codex audit 2026-10-16, F12).
# The live catalogue has "Greatest Hits" by 21 different acts.


@dataclass(frozen=True)
class Asked:
    """Whose album is being asked about, and how a source's credit is read.

    *alias*: a source credit -> the name MUSAEUS files it under, from an
    EXPLICIT artist_canon.tsv entry only (ArtistCanon.resolve_exact): the
    canon is what shortened "Tom Petty And The Heartbreakers" to Tom Petty
    and filed Wings under Paul McCartney on the way in, so it reads the
    sources' credits the same way (review of #199, finding 1).

    *composer*: set when the row is filed under a composer. Grey, 2026-10-17:
    "for classical albums only composer instead of artists" -- a source's
    answer is that composer's album only if one of its credited names IS the
    composer (Composer_Canon.tsv); the performers never count.
    """

    artist: str
    alias: Callable[[str], str | None] = field(default=lambda s: None)
    composer: str | None = None
    composer_canon: Mapping[str, str] = field(default_factory=dict)


_FOLD = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "ß": "ss", "ł": "l",
                       "Ł": "L", "đ": "d", "Đ": "D", "œ": "oe", "Œ": "OE"})  # fmt: skip
#: Separators between the names in one credit. Never inside a name: a
#: composer-named ensemble ("Franz Liszt Chamber Orchestra") stays whole.
_SPLIT = re.compile(r"\s*(?:,|;|/|\+|&|\band\b|\bfeat\.?|\bfeaturing\b|\bwith\b)\s*", re.I)
#: A backing band or ensemble after the lead act: "X & The Y", "X and Her
#: Orchestra", "X, The Y", "X Quintet". Never applied to a composer.
_BACKING = re.compile(r"(?:\s+(?:&|and|\+)\s+|,\s+)(?:the|his|her)\s+.+$", re.I)
_ENSEMBLE = re.compile(
    r"\s+(?:band|orchestra|trio|quartet|quintet|sextet|septet|octet|ensemble|combo)$", re.I
)


def _key(name: str) -> str:
    """act_key, with the letters NFKD cannot take apart folded first, and a
    plain case-folded name when act_key leaves nothing ("坂本龍一")."""
    folded = (name or "").translate(_FOLD)
    return act_key(folded) or folded.casefold().strip()


def _lead(name: str) -> str:
    lead = _ENSEMBLE.sub("", _BACKING.sub("", name or "").strip()).strip()
    return lead if _key(lead) else name


def _parts(credit: str) -> list[str]:
    return [p for p in _SPLIT.split(credit or "") if p.strip()]


def _is_ours(credit: str, asked: Asked) -> bool:
    """Is a source's *credit* the act we asked about?"""
    if not credit or not asked.artist:
        return False
    if asked.composer:
        want = _key(asked.composer)
        return any(
            asked.composer_canon.get(part.strip().lower()) == asked.composer or _key(part) == want
            for part in _parts(credit)
        )
    ours = {_key(asked.artist), _key(_lead(asked.artist)), _key(folder_artist(asked.artist))}
    theirs: set[str] = set()
    for whole in {credit, asked.alias(credit) or credit}:
        theirs |= {_key(whole), _key(_lead(whole)), _key(folder_artist(whole))}
    # Any one name in their credit that is our act: "Queen + Adam Lambert",
    # "Johnny Mathis & Deniece Williams" asked for Deniece Williams.
    theirs |= {_key(_lead(asked.alias(p) or p)) for p in _parts(credit)}
    return bool((ours & theirs) - {""})


def _itunes_lists(collection_id: object, asked: Asked) -> bool:
    """Is our act on this iTunes release? For one credited to someone else --
    "Various Artists", a soundtrack's composer -- the track list is the only
    evidence it is ours: the Grosse Pointe Blank soundtrack, not another
    act's "Gold"."""
    if not collection_id:
        return False
    data = _get_json(
        "https://itunes.apple.com/lookup?"
        + urlencode({"id": collection_id, "entity": "song", "limit": 200})
    )
    return any(
        _is_ours(r.get("artistName", ""), asked)
        for r in (data or {}).get("results") or []
        if r.get("wrapperType") == "track"
    )


def _deezer_lists(album_id: object, asked: Asked) -> bool:
    """The Deezer twin of _itunes_lists."""
    if not album_id:
        return False
    data = _get_json(f"https://api.deezer.com/album/{album_id}/tracks?limit=200")
    return any(
        _is_ours(((t.get("artist") or {}).get("name", "")), asked)
        for t in (data or {}).get("data") or []
    )


def _ours(
    results: list, asked: Asked, credited: Callable[[dict], str], lists: Callable[[dict], bool]
) -> Iterator[dict]:
    """The results that are our act's, best first, looked at lazily.

    Credited to the act first -- their covers are tried before any track
    list is fetched. Then any other same-title release whose track list
    names the act. A track-list lookup that fails is passed over, not fatal:
    a cover already found must not be lost to it, and "could not ask" is
    raised only when nothing was found (review of #199, finding 2).
    Composer rows take no track-list evidence: the credit must name the
    composer.
    """
    own = [r for r in results if _is_ours(credited(r), asked)]
    yield from own
    if asked.composer:
        return
    failed: ArtUnavailable | None = None
    for r in results:
        if any(r is o for o in own):
            continue
        try:
            listed = lists(r)
        except ArtUnavailable as exc:
            failed = exc
            continue
        if listed:
            yield r
    if failed is not None:
        raise failed


# ── sources ───────────────────────────────────────────────────────────────────


def fetch_itunes(artist: str, album: str, asked: Asked | None = None) -> bytes | None:
    """iTunes Search. No key and no MBID needed, so it is tried first."""
    asked = asked or Asked(artist)
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
    for r in _ours(
        ordered,
        asked,
        lambda r: r.get("artistName", ""),
        lambda r: bool(want) and _itunes_lists(r.get("collectionId"), asked),
    ):
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


def fetch_deezer(artist: str, album: str, asked: Asked | None = None) -> bytes | None:
    """Deezer search. Keyless like iTunes, and `cover_xl` is 1000x1000.

    Added 2026-08-31 when Grey asked about Amazon as a source. Amazon has no
    open cover endpoint -- its Product Advertising API needs an Associates
    account with qualifying sales -- so this fills the same gap: a second
    keyless, high-resolution source that answers when iTunes does not.
    """
    asked = asked or Asked(artist)
    if not artist:
        return None
    term = f"{artist} {album}".strip()
    data = _get_json("https://api.deezer.com/search/album?" + urlencode({"q": term, "limit": 5}))
    if not data:
        return None
    want = _norm(album)
    results = data.get("data", []) or []
    ordered = [r for r in results if want and _norm(r.get("title", "")) == want]
    if not ordered:
        ordered = results if not want else []
    for r in _ours(
        ordered,
        asked,
        lambda r: (r.get("artist") or {}).get("name", ""),
        lambda r: bool(want) and _deezer_lists(r.get("id"), asked),
    ):
        for key in ("cover_xl", "cover_big"):
            u = r.get(key)
            if not u:
                continue
            blob = _get(u)
            if _looks_like_image(blob):
                return blob
    return None


def fetch_coverartarchive(artist: str, album: str, asked: Asked | None = None) -> bytes | None:
    """Cover Art Archive, via the RELEASE-GROUP mbid.

    The release-group is found by searching MusicBrainz for artist + album,
    so this needs no pre-existing MBID in our own database.
    """
    asked = asked or Asked(artist)
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
        # A scored search, like the other two: the best-scoring release group
        # can be another act's. Its credit must be this act -- whole, or any
        # one name in it ("Paul McCartney & Wings").
        credit = rg.get("artist-credit") or []
        whole = "".join(
            (c.get("name") or "") + (c.get("joinphrase") or "")
            for c in credit
            if isinstance(c, dict)
        )
        names = [c.get("name") or "" for c in credit if isinstance(c, dict)]
        if not (_is_ours(whole, asked) or any(_is_ours(n, asked) for n in names)):
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
        {
            "method": "album.getinfo",
            "api_key": api_key,
            "artist": artist,
            "album": album,
            "format": "json",
        }
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
    artist: str,
    album: str,
    lastfm_key: str = "",
    min_edge: int = MIN_EDGE_PX,
    asked: Asked | None = None,
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

    `asked`: how a source's credit is read -- the artist canon, and the
    composer for a classical row (see Asked). Without it, the stored artist
    as written.
    """

    def ask(fn: Callable[..., bytes | None]) -> Callable[[], bytes | None]:
        return (lambda: fn(artist, album, asked)) if asked else (lambda: fn(artist, album))

    attempts = 0
    unavailable = 0
    best: tuple[bytes, str] | None = None  # largest sub-threshold fallback
    # Only a source that can be asked counts: with no Last.fm key, or no album
    # for the Cover Art Archive, nothing was asked, and counting those as
    # answers turned "every source refused" into "no art exists" (review of
    # slice A, finding 12).
    for name, can_ask, fn in (
        ("itunes", bool(artist), ask(fetch_itunes)),
        ("deezer", bool(artist), ask(fetch_deezer)),
        ("coverartarchive", bool(artist and album), ask(fetch_coverartarchive)),
        ("lastfm", bool(lastfm_key and artist and album),
         lambda: fetch_lastfm(artist, album, lastfm_key)),
    ):  # fmt: skip
        if not can_ask:
            continue
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
            logger.debug(
                "[art] %s gave %s for %r / %r -- holding, trying next",
                name,
                describe(blob),
                artist,
                album,
            )
            # Keep the LARGEST sub-threshold image, not the first one seen:
            # if nothing clears the floor we still want the best on offer.
            if best is None or _longest_edge(blob) > _longest_edge(best[0]):
                best = (blob, name)
            continue
        logger.info("[art] %s supplied %s for %r / %r", name, describe(blob), artist, album)
        return blob, name

    if best is not None:
        logger.info(
            "[art] only undersized art found for %r / %r: %s from %s",
            artist,
            album,
            describe(best[0]),
            best[1],
        )
        return best
    if attempts and unavailable == attempts:
        raise ArtUnavailable("no art source answered")
    return None


# ── artist picture: when no album cover can be found ──────────────────────────
#
# Grey, 2026-10-06: "search the web and Wikipedia for pictures if album art
# search is unsuccessful". Picking by hand that day showed what goes wrong:
# Deezer's first exact-name match was a rap act for Bad Company, a DJ for
# Fergie, album art for Handel. So Wikipedia comes first and only a page that
# says it is about music counts; Deezer second, the exact name with the most
# fans, never its blank placeholder.

_MUSIC_WORDS = re.compile(
    r"\b(band|singer|musician|composer|songwriter|rapper|group|duo|trio|quartet|"
    r"orchestra|ensemble|guitarist|pianist|violinist|cellist|drummer|bassist|vocalist|"
    r"dj|conductor|music|choir|soprano|mezzo|tenor|baritone|bandleader)\b",
    re.IGNORECASE,
)
_WIKI_SUFFIXES = ("", " (band)", " (musician)", " (singer)")


def _lead_artist(artist: str) -> str:
    """The first artist of a credit: "Linda Ronstadt, James Ingram" -> "Linda Ronstadt"."""
    return re.split(r"\s*(?:,|&|\bfeat\.?|\bfeaturing\b|\bwith\b)\s*", artist, flags=re.I)[
        0
    ].strip()


def fetch_wikipedia_artist(artist: str) -> bytes | None:
    """The lead picture of the artist's English Wikipedia page, if the page is about music."""
    for suffix in _WIKI_SUFFIXES:
        title = quote((artist + suffix).replace(" ", "_"))
        data = _get_json(f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}")
        if not data or data.get("type") != "standard":
            continue  # missing, or a disambiguation page
        if not _MUSIC_WORDS.search(data.get("description") or ""):
            continue  # an actor, a footballer, a place
        url = (data.get("originalimage") or data.get("thumbnail") or {}).get("source")
        blob = _get(url) if url else None
        if _looks_like_image(blob):
            return blob
    return None


def fetch_deezer_artist(artist: str) -> bytes | None:
    """Deezer's picture of the artist: the exact name with the most fans."""
    data = _get_json(
        "https://api.deezer.com/search/artist?" + urlencode({"q": artist, "limit": 10})
    )
    if not data:
        return None
    want = _norm(artist)
    named = [
        r for r in data.get("data", [])
        if _norm(r.get("name", "")) == want and r.get("picture_xl") and "/artist//" not in r["picture_xl"]
    ]  # fmt: skip
    if not named:
        return None
    best = max(named, key=lambda r: int(r.get("nb_fan") or 0))
    blob = _get(best["picture_xl"])
    return blob if _looks_like_image(blob) else None


def fetch_artist_picture(artist: str) -> tuple[bytes, str] | None:
    """A picture of the artist or group, with its source; None when none is found.

    Raises ArtUnavailable only when nothing could be asked at all.
    """
    lead = _lead_artist(artist)
    if not lead:
        return None
    asked = unavailable = 0
    for name, fn in (("wikipedia", fetch_wikipedia_artist), ("deezer", fetch_deezer_artist)):
        asked += 1
        try:
            blob = fn(lead)
        except ArtUnavailable as exc:
            unavailable += 1
            logger.debug("[art] %s unavailable for artist %r: %s", name, lead, exc)
            continue
        if blob:
            logger.info("[art] %s supplied a picture of %r (%s)", name, lead, describe(blob))
            return blob, f"artist picture ({name})"
    if unavailable == asked:
        raise ArtUnavailable(f"no source could be asked for a picture of {lead!r}")
    return None
