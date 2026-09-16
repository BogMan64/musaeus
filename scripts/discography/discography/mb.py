"""Asking MusicBrainz for an artist's release groups.

BORROWED, NOT COPIED
--------------------
MUSAEUS already talks to MusicBrainz in musaeus/stages/mb_enrich.py, with a
User-Agent, a 1.1s rate limit and a 503 retry. This borrows its _mb_get rather
than writing a third HTTP client, and that matters more here than convenience:

  - MusicBrainz identifies and blocks clients by User-Agent. Two different
    User-Agents from one machine is two reputations to keep clean, and a
    misbehaving one gets the IP blocked, not the string.
  - The rate limit is per IP, not per process. Two clients each politely waiting
    1.1s still send two requests a second between them.
  - _mb_get calls MUSAEUS's network_policy gateway, so LOCAL_ONLY applies to this
    tool too. Writing a private client would silently escape a setting Grey set
    deliberately.

_mb_get does NOT sleep -- mb_enrich's callers do that themselves -- so the
throttle below is this module's own responsibility.

The fallback is visible for the same reason as fmradio/titles.py: a silent
fallback to a private client would quietly drop all three properties above.
"""

from __future__ import annotations

import contextlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field

from .scope import ReleaseGroup

#: MusicBrainz asks for ≤1 request/second unauthenticated. 1.1 matches MUSAEUS.
RATE_LIMIT_S = 1.1

#: MB caps browse results at 100 per page.
PAGE_SIZE = 100

#: Hard stop on paging. An artist with more release groups than this is a
#: compilation-farm entry like "Various Artists" and paging it to the end would
#: spend minutes at one request per second for a result nobody reads.
MAX_PAGES = 12

IMPLEMENTATION = "builtin"
IMPORT_ERROR: str | None = None

_musaeus_get = None
try:  # pragma: no cover - depends on the machine
    from musaeus.stages.mb_enrich import _mb_get as _musaeus_get  # type: ignore

    IMPLEMENTATION = "musaeus"
except Exception as exc:
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    _musaeus_get = None

_FALLBACK_UA = (
    "discography-lab/0.1 ( https://github.com/BogMan64/musaeus ) "
    "python-urllib standalone-fallback"
)


class Unavailable(RuntimeError):
    """MusicBrainz could not be reached. Recorded, never silently emptied.

    Distinct from "this artist has no release groups". Conflating them would
    report a complete discography for an artist whose lookup failed, which is
    the most damaging error this tool could make: it says "you own everything"
    when nothing was checked.
    """


def provenance() -> str:
    if IMPLEMENTATION == "musaeus":
        return "MusicBrainz client: MUSAEUS mb_enrich._mb_get (shared User-Agent, rate limit, network policy)"
    return f"MusicBrainz client: standalone fallback -- MUSAEUS not used ({IMPORT_ERROR})"


@contextlib.contextmanager
def allow_network() -> Iterator[str]:
    """Ask MUSAEUS's network gateway for permission, scoped to this block.

    Borrowing _mb_get means inheriting MUSAEUS's network policy, whose default is
    LOCAL_ONLY -- "preview and any unattended default". The first live run of this
    tool was refused by it on all three artists, which is the gateway working:
    it exists because auditing call sites does not hold as new ones appear.

    So permission is REQUESTED rather than routed around. Two properties matter:

      - Scoped, via network_policy.policy() rather than set_policy(). Its own
        docstring records a P0-14 test that passed alone and failed in the full
        suite because a caller set ALLOWED and never put it back, leaving
        everything after it in the process permissive.
      - Announced. The caller logs what this returns, so a run that goes online
        says so. Quietly overriding a policy Grey set deliberately would be worse
        than being refused.

    Yields a one-line description of what was actually arranged.
    """
    try:
        from musaeus.network_policy import NetworkPolicy, policy  # type: ignore
    except Exception as exc:
        yield f"no MUSAEUS network policy to satisfy ({type(exc).__name__})"
        return
    with policy(NetworkPolicy.ALLOWED):
        yield (
            "MUSAEUS network policy: temporarily ALLOWED for this run "
            "(scoped, restored on exit)"
        )


def _fallback_get(path: str, params: dict[str, str]) -> dict:
    url = f"https://musicbrainz.org/ws/2/{path}?" + urllib.parse.urlencode(
        {**params, "fmt": "json"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": _FALLBACK_UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise Unavailable(f"MusicBrainz refused the request ({exc.code})") from exc
        raise Unavailable(f"MusicBrainz HTTP {exc.code}") from exc
    except Exception as exc:
        raise Unavailable(f"{type(exc).__name__}: {exc}") from exc


@dataclass
class MusicBrainz:
    """Release groups for an artist, paged and throttled."""

    sleep_s: float = RATE_LIMIT_S
    _last: float = field(default=0.0, repr=False)
    calls: int = 0

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.sleep_s:
            time.sleep(self.sleep_s - gap)
        self._last = time.monotonic()

    def _get(self, path: str, params: dict[str, str]) -> dict:
        self._throttle()
        self.calls += 1
        if _musaeus_get is not None:
            try:
                return _musaeus_get(path, params)
            except urllib.error.HTTPError as exc:
                raise Unavailable(f"MusicBrainz HTTP {exc.code}") from exc
            except Unavailable:
                raise
            except Exception as exc:
                # Includes MUSAEUS's own network-policy refusal under LOCAL_ONLY,
                # which is a legitimate "cannot ask" and must not read as "no
                # release groups".
                raise Unavailable(f"{type(exc).__name__}: {exc}") from exc
        return _fallback_get(path, params)

    def release_groups(self, artist_mbid: str) -> list[ReleaseGroup]:
        """Every release group credited to this artist, all types.

        Filtering to studio albums happens in scope.py, not here, so the
        excluded count stays visible. A client that pre-filtered would make the
        ruling impossible to check -- and the excluded count is the number that
        shows the ruling is working at all.
        """
        out: list[ReleaseGroup] = []
        offset = 0
        for _ in range(MAX_PAGES):
            data = self._get(
                "release-group",
                {"artist": artist_mbid, "limit": str(PAGE_SIZE), "offset": str(offset)},
            )
            groups = data.get("release-groups")
            if not isinstance(groups, list):
                raise Unavailable(
                    "MusicBrainz returned no release-groups list for "
                    f"{artist_mbid} -- got keys {sorted(data)[:5]}"
                )
            for g in groups:
                if not isinstance(g, dict):
                    continue
                out.append(
                    ReleaseGroup(
                        mbid=g.get("id") or "",
                        title=(g.get("title") or "").strip(),
                        primary_type=(g.get("primary-type") or ""),
                        secondary_types=tuple(g.get("secondary-types") or ()),
                        first_release_date=(g.get("first-release-date") or ""),
                    )
                )
            total = data.get("release-group-count")
            offset += PAGE_SIZE
            if not isinstance(total, int) or offset >= total:
                break
        return out
