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
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .scope import ReleaseGroup

DEFAULT_CACHE = Path.home() / ".cache" / "discography-lab" / "mb_cache.db"


class ReleaseGroupCache:
    """SQLite cache for MusicBrainz release-group results.

    Keyed by artist MBID. A cached result is the full list of release groups as
    JSON; a failure record is kept separately so a transient 503 does not
    permanently suppress an artist while a deliberate "not found" does.

    ORPHEUS cached its discography results because the fetch was slow. This
    is the same reason: 63 artists at 1.1s/request minimum, with paging, is
    10-15 minutes per run. Without a cache every run re-fetches everything.

    Commits per row (not per run) so a keyboard interrupt or timeout mid-run
    still saves the work already done. The next run picks up where it left off.
    """

    def __init__(self, path: Path = DEFAULT_CACHE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS release_groups (
                artist_mbid  TEXT PRIMARY KEY,
                fetched_at   TEXT DEFAULT (datetime('now')),
                payload      TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS failures (
                artist_mbid  TEXT PRIMARY KEY,
                failed_at    TEXT DEFAULT (datetime('now')),
                reason       TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def get(self, artist_mbid: str) -> list[dict] | None:
        """Cached release groups or None if not cached."""
        row = self._conn.execute(
            "SELECT payload FROM release_groups WHERE artist_mbid = ?",
            (artist_mbid,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload"])

    def put(self, artist_mbid: str, groups: list[dict]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO release_groups (artist_mbid, payload) VALUES (?, ?)",
            (artist_mbid, json.dumps(groups)),
        )
        # Also clear any prior failure record -- if we got data now, a cached
        # failure from a transient 503 should not suppress this artist next run.
        self._conn.execute(
            "DELETE FROM failures WHERE artist_mbid = ?", (artist_mbid,)
        )
        self._conn.commit()

    def note_failure(self, artist_mbid: str, reason: str) -> None:
        """Record a lookup failure. NOT used for transient errors (503, timeout).

        Only permanent failures (404, artist genuinely not found) are cached.
        A transient failure should retry on the next run, not be suppressed.
        """
        self._conn.execute(
            "INSERT OR REPLACE INTO failures (artist_mbid, reason) VALUES (?, ?)",
            (artist_mbid, reason),
        )
        self._conn.commit()

    def stats(self) -> str:
        n_ok = self._conn.execute("SELECT COUNT(*) FROM release_groups").fetchone()[0]
        n_fail = self._conn.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
        return f"{n_ok} cached, {n_fail} failures"

    def close(self) -> None:
        self._conn.close()

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


_RETRY_CODES = (503, 429)
_MAX_RETRIES = 3
_RETRY_WAIT_S = 5.0


def _fallback_get(path: str, params: dict[str, str]) -> dict:
    url = f"https://musicbrainz.org/ws/2/{path}?" + urllib.parse.urlencode(
        {**params, "fmt": "json"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": _FALLBACK_UA})
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                # Permanent auth failure -- no point retrying.
                raise Unavailable(f"MusicBrainz refused the request ({exc.code})") from exc
            if exc.code in _RETRY_CODES and attempt < _MAX_RETRIES - 1:
                # Transient -- back off and retry.
                time.sleep(_RETRY_WAIT_S * (attempt + 1))
                last_exc = exc
                continue
            raise Unavailable(f"MusicBrainz HTTP {exc.code}") from exc
        except Exception as exc:
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_WAIT_S * (attempt + 1))
                last_exc = exc
                continue
            raise Unavailable(f"{type(exc).__name__}: {exc}") from exc
    raise Unavailable(f"MusicBrainz unreachable after {_MAX_RETRIES} attempts: {last_exc}")


@dataclass
class MusicBrainz:
    """Release groups for an artist, paged, throttled, and cached.

    Cache is keyed by artist MBID and persists across runs, so a 63-artist
    run that times out at artist 40 resumes from artist 41 next time rather
    than re-fetching the first 40. ORPHEUS cached for the same reason.
    """

    sleep_s: float = RATE_LIMIT_S
    cache: ReleaseGroupCache = field(default_factory=ReleaseGroupCache)
    _last: float = field(default=0.0, repr=False)
    calls: int = 0
    cache_hits: int = 0

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

        Checks the cache first. On a cache hit no network request is made and
        the throttle is not invoked -- the point of caching is to avoid the
        cost entirely, not just to avoid it once.

        Filtering to studio albums happens in scope.py, not here, so the
        excluded count stays visible. A client that pre-filtered would make the
        ruling impossible to check -- and the excluded count is the number that
        shows the ruling is working at all.
        """
        cached = self.cache.get(artist_mbid)
        if cached is not None:
            self.cache_hits += 1
            return [
                ReleaseGroup(
                    mbid=g.get("mbid") or "",
                    title=g.get("title") or "",
                    primary_type=g.get("primary_type") or "",
                    secondary_types=tuple(g.get("secondary_types") or ()),
                    first_release_date=g.get("first_release_date") or "",
                )
                for g in cached
            ]

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

        # Cache the result as plain dicts -- ReleaseGroup is not JSON-serialisable
        # directly, and storing the raw fields means the deserialiser above can
        # reconstruct them without needing the scope module at read time.
        self.cache.put(artist_mbid, [
            {
                "mbid": rg.mbid,
                "title": rg.title,
                "primary_type": rg.primary_type,
                "secondary_types": list(rg.secondary_types),
                "first_release_date": rg.first_release_date,
            }
            for rg in out
        ])
        return out

    def close(self) -> None:
        self.cache.close()
