"""Metadata sources, behind an interface, so tests never touch the network.

THE RULE THIS FILE EXISTS TO ENFORCE: an error is never data.

On 2026-09-10 an unauthenticated request to ListenBrainz's popularity endpoint
returned HTTP 200 with 9,057 real Beatles records -- from an edge cache -- while
the same endpoint returned 401 for every other artist:

    {"code":401,"error":"Due to bad actors and AI scrapers causing undue
     traffic on our sites, you need to provide an Auth token for this
     endpoint. Sorry for this mess."}

One cached hit made an authenticated endpoint look public. Had the client
shrugged at 401 and returned [], every artist would have scored as "no
ListenBrainz signal", the run would have completed, and the report would have
said success having measured nothing. That is the failure this whole project
catalogues, so:

  - 401/403 raise AuthRequired and stop the run. Not skipped, not logged.
  - a transport failure raises SourceUnavailable and is recorded against the
    artist, so the CSV can say "not asked" rather than "nothing found".
  - "no data" is only ever returned when the source answered successfully and
    had nothing.

Absence of an answer and an answer of absence are different facts and this
file will not let them merge.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .model import Candidate
from .popularity import ABSOLUTE_NOISE_FLOOR

USER_AGENT = "fm-radio-lab/0.1 ( https://github.com/BogMan64/musaeus )"

#: Both services ask for ONE call per second and will block a client that
#: ignores it.
#:
#: LB_MIN_INTERVAL was 0.35 on my assumption that ListenBrainz was more
#: generous than MusicBrainz. Their documentation says otherwise, in capitals:
#: "All users of the API must ensure that each of their client applications
#: never make more than ONE call per second." Corrected 2026-09-10 after
#: reading rather than assuming.
MB_MIN_INTERVAL = 1.1
LB_MIN_INTERVAL = 1.1

#: ListenBrainz returns its own rate-limit accounting in response headers, and
#: obeying the server beats guessing with a fixed sleep. Their docs recommend
#: Reset-In over Reset specifically because it is resilient against a client
#: with a wrong clock -- a nice detail, and the reason this reads seconds
#: remaining rather than an epoch.
RATE_HEADERS = ("X-RateLimit-Remaining", "X-RateLimit-Reset-In")

#: Below this, a ListenBrainz record is noise rather than a pressing anyone
#: heard. Measured on The Beatles: 9,057 records returned, 2,249 of them with
#: exactly ONE listen -- session takes, Ed Sullivan performances, "A/B Road:
#: Complete Get Back Sessions". Only 1,753 cleared 100. Feeding the tail to
#: the scorer would bury the real pressings under bootlegs.
#:
#: Configurable rather than baked in: the right floor for The Beatles is not
#: the right floor for an artist with 200 total listens across their catalogue.
#:
#: REVISED. This was 100, applied here at fetch time, and that was doing two
#: jobs at once: keeping bootleg noise out of the scorer (wanted) and deciding
#: an artist was too obscure to consider at all (not wanted). An artist whose
#: best recording sits at 40 listens returned nothing and was reported as "no
#: ListenBrainz signal" -- the same message an outage produces.
#:
#: So the number kept here is now only a noise gate, and the real separation of
#: pressings from bootlegs is a per-artist top-decile floor computed in
#: fmradio/popularity.py, where the artist's own distribution is known. Fetching
#: more and filtering later also costs nothing: the response is one request per
#: artist either way, and the cache is local.
DEFAULT_MIN_LISTENS = ABSOLUTE_NOISE_FLOOR


class AuthRequired(RuntimeError):
    """The source needs a credential we do not have. Fatal, never skipped."""


class SourceUnavailable(RuntimeError):
    """The source could not be reached. Recorded, never silently emptied."""


class PopularitySource(Protocol):
    def top_recordings(self, artist_mbid: str) -> list[dict]: ...


class PressingSource(Protocol):
    def pressings(self, artist: str, title: str) -> list[dict]: ...


# ── Real clients ──────────────────────────────────────────────────────────────


class _Throttled:
    """Shared rate limiting. One implementation, not one per client."""

    def __init__(self, min_interval: float) -> None:
        self._min = min_interval
        self._last = 0.0
        #: Set when the server says its window is exhausted. Absolute, so a
        #: long gap between calls costs nothing.
        self._sleep_until = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if self._sleep_until > now:
            time.sleep(self._sleep_until - now)
            self._sleep_until = 0.0
            now = time.monotonic()
        gap = now - self._last
        if gap < self._min:
            time.sleep(self._min - gap)
        self._last = time.monotonic()

    def get_json(self, url: str, headers: dict[str, str]) -> object:
        self.wait()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                # Let the server set the pace where it tells us. Its accounting
                # is authoritative; ours is a guess that happens to be close.
                remaining = resp.headers.get("X-RateLimit-Remaining")
                reset_in = resp.headers.get("X-RateLimit-Reset-In")
                if remaining is not None and reset_in is not None:
                    try:
                        if int(remaining) <= 1:
                            self._sleep_until = time.monotonic() + float(reset_in)
                    except ValueError:
                        pass  # a header we cannot parse is not a reason to stop
                return body
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                detail = ""
                try:
                    detail = exc.read()[:300].decode("utf-8", "replace")
                except Exception:
                    pass
                raise AuthRequired(f"HTTP {exc.code} from {url}: {detail}") from exc
            raise SourceUnavailable(f"HTTP {exc.code} from {url}") from exc
        except Exception as exc:
            raise SourceUnavailable(f"{type(exc).__name__} from {url}: {exc}") from exc


@dataclass
class ListenBrainz:
    """Popularity. One request per artist returns the whole catalogue ranked.

    Measured: The Beatles come back as 9,057 records already sorted by listen
    count, "Let It Be" at 2,093,966 first. So this is not a top-N with a cap --
    it is everything, and the ordering is usable directly.
    """

    token: str
    min_listens: int = DEFAULT_MIN_LISTENS
    _http: _Throttled = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if not (self.token or "").strip():
            raise AuthRequired(
                "no ListenBrainz user token. Get one from listenbrainz.org "
                "settings and put it in LISTENBRAINZ_USER_TOKEN, or in "
                "/mnt/NUC8TB_BACKUP/SECRETS/listenbrainz.env. Note this is a "
                "USER TOKEN -- not a MetaBrainz OAuth client id/secret, and "
                "not a MetaBrainz access token; those are different "
                "credentials for different services."
            )
        self._http = _Throttled(LB_MIN_INTERVAL)

    def top_recordings(self, artist_mbid: str) -> list[dict]:
        url = (
            "https://api.listenbrainz.org/1/popularity/"
            f"top-recordings-for-artist/{urllib.parse.quote(artist_mbid)}"
        )
        data = self._http.get_json(url, {"Authorization": f"Token {self.token}"})
        if not isinstance(data, list):
            raise SourceUnavailable(f"expected a list from {url}, got {type(data).__name__}")
        return [
            r
            for r in data
            if isinstance(r, dict) and (r.get("total_listen_count") or 0) >= self.min_listens
        ]


@dataclass
class MusicBrainz:
    """Pressings. No token -- a User-Agent and one request per second.

    Only ever asked about songs pass 1 could not settle, which is the whole
    point of the two-pass split.
    """

    _http: _Throttled = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._http = _Throttled(MB_MIN_INTERVAL)

    def pressings(self, artist: str, title: str) -> list[dict]:
        query = f'artist:"{_escape(artist)}" AND recording:"{_escape(title)}"'
        url = (
            "https://musicbrainz.org/ws/2/recording?fmt=json&limit=100&query="
            + urllib.parse.quote(query)
        )
        data = self._http.get_json(url, {})
        if not isinstance(data, dict):
            raise SourceUnavailable(f"expected an object from {url}")
        return list(data.get("recordings") or [])


def _escape(s: str) -> str:
    """Lucene special characters, so a title containing them is not a syntax
    error. MUSAEUS hit this on artists with '&' and '/' in the name."""
    out = s.replace("\\", "\\\\")
    for ch in '+-!(){}[]^"~*?:':
        out = out.replace(ch, "\\" + ch)
    return out


# ── Adapters into the scorer's own vocabulary ─────────────────────────────────


def candidate_from_listenbrainz(rec: dict) -> Candidate:
    """A ListenBrainz record carries play counts and a length, and no date or
    release-group type. Those stay empty rather than guessed -- the scorer
    reports "no first-release-date" and lets pass 2 supply it."""
    ms = rec.get("length")
    return Candidate(
        title=rec.get("recording_name") or "",
        release_title=rec.get("release_name") or "",
        length_seconds=(ms / 1000.0) if isinstance(ms, (int, float)) and ms else None,
        listen_count=rec.get("total_listen_count"),
        listener_count=rec.get("total_user_count"),
        recording_mbid=rec.get("recording_mbid") or "",
        release_mbid=rec.get("release_mbid") or "",
    )


def candidate_from_musicbrainz(rec: dict, release: dict) -> Candidate:
    """One MusicBrainz recording appears on many releases; each pairing is a
    distinct pressing, so this takes both.

    `first-release-date` is read from the RECORDING, falling back to the
    release group. MetaBrainz's own docs warn these differ: %originalyear% is
    the release group's date, not the recording's, so a 1975 recording on a
    1990 Greatest Hits reads as 1990. MUSAEUS's original_year.py takes the
    minimum of the two for the same reason.
    """
    rg = release.get("release-group") or {}
    recording_date = rec.get("first-release-date") or ""
    group_date = rg.get("first-release-date") or ""
    dates = [d for d in (recording_date, group_date) if d]
    ms = rec.get("length")
    media = release.get("media") or []
    fmt = ""
    for m in media:
        if m.get("format"):
            fmt = m["format"]
            break
    return Candidate(
        title=rec.get("title") or "",
        disambiguation=rec.get("disambiguation") or "",
        release_title=release.get("title") or "",
        release_group_type=rg.get("primary-type") or "",
        secondary_types=tuple(rg.get("secondary-types") or ()),
        first_release_date=min(dates) if dates else "",
        length_seconds=(ms / 1000.0) if isinstance(ms, (int, float)) and ms else None,
        media_format=fmt,
        country=release.get("country") or "",
        recording_mbid=rec.get("id") or "",
        release_mbid=release.get("id") or "",
    )


# ── Fakes, for tests ──────────────────────────────────────────────────────────


@dataclass
class FakePopularity:
    """Replays recorded responses. Keyed by artist_mbid."""

    responses: dict[str, list[dict]]
    raise_auth: bool = False
    raise_unavailable: bool = False
    calls: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    def top_recordings(self, artist_mbid: str) -> list[dict]:
        self.calls.append(artist_mbid)
        if self.raise_auth:
            raise AuthRequired("fake: token rejected")
        if self.raise_unavailable:
            raise SourceUnavailable("fake: unreachable")
        return self.responses.get(artist_mbid, [])


@dataclass
class FakePressings:
    responses: dict[tuple[str, str], list[dict]]
    raise_unavailable: bool = False
    calls: list[tuple[str, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    def pressings(self, artist: str, title: str) -> list[dict]:
        self.calls.append((artist, title))
        if self.raise_unavailable:
            raise SourceUnavailable("fake: unreachable")
        return self.responses.get((artist, title), [])
