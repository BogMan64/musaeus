#!/usr/bin/env python3
"""
MUSAEUS — Discogs artist lookup, a secondary attempt when MusicBrainz has none.

Why this exists
----------------
mb_enrich asks MusicBrainz for every artist and, on 2026-09-03, 499 of
10,444 catalogued rows came back "asked, no confident match" -- MB
genuinely does not have that act. Those rows never get an mb_artist_id,
so identity_tag.py silently skips writing any identity into their file
tags forever: ~4.8% of the library permanently unlinkable to a canonical
artist by anything that reads MBIDs.

Discogs indexes a lot that MusicBrainz does not -- vinyl-only pressings,
regional releases, small/self-released acts -- so it is a reasonable
second opinion for exactly the rows MB already gave up on. NOT a
replacement for MB and not tried first: MB is free, needs no key, and is
the correct source of truth for MBIDs. This is what runs after it says no.

Deliberately separate columns, not reused MB ones
---------------------------------------------------
archive.mb_artist_id / mb_artist_name are consumed by identity_tag.py,
which writes them into the file's own tags as if they were real
MusicBrainz identifiers -- because they are. A Discogs artist ID is a
different namespace entirely (Discogs's own numeric IDs, not MBIDs), and
writing one into mb_artist_id would corrupt every downstream reader that
expects an MBID there -- Apple Music, Plex, mp3tag, anything MB-aware.
So this stores discogs_artist_id / discogs_artist_name / discogs_checked_at
instead. Whether anything should ever TAG a file with a Discogs identity
is a separate, larger decision, not made here.

Design, mirrored from mb_enrich.py on purpose
------------------------------------------------
Same three-state shape as MusicBrainz lookups, and for the identical
reason: a network wobble must never be recorded as "asked, not found",
because that answer is permanent (nothing re-queries a stamped row).

    (discogs_id, name)   -- found
    None                 -- asked, definitively not found  -> stamp
    LookupUnavailable    -- never asked successfully        -> leave alone

Auth: `Authorization: Discogs token=<key>`. Rate limit: 60 req/min
authenticated (Discogs's own published number); rate-limited to a
conservative 1.1 s between requests here, matching mb_enrich's own MB
rate limit rather than trying to hug the ceiling.
"""

from __future__ import annotations

import json
import logging
import urllib.error
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .network_policy import check as _network_check

logger = logging.getLogger(__name__)

_DISCOGS_BASE = "https://api.discogs.com"
_USER_AGENT = "MUSAEUS/1.0 +https://github.com/BogMan64/musaeus"
_TIMEOUT_S = 15
_RETRY_WAIT_S = 5

#: Conservative floor between requests. Discogs's own published limit is
#: 60/min authenticated (1 per second); matching mb_enrich's MB rate limit
#: rather than trying to run right up against the ceiling.
RATE_LIMIT_S = 1.1

#: Discogs requires a score this confident before a match is trusted --
#: mirrors mb_enrich._ARTIST_SCORE's reasoning: a name merely CONTAINING
#: the query is not the same artist. Discogs search results are not
#: consistently scored the way MusicBrainz's are, so this is applied to
#: the exact-name-match test below rather than to a numeric field.


class LookupUnavailable(Exception):
    """Discogs gave no answer: timeout, 5xx, DNS, auth failure, or a
    policy refusal. Distinct from "asked, and Discogs has no such artist"
    -- see the module docstring's three-state contract. Conflating the two
    would let a transport failure get cached as a permanent miss."""


class DiscogsAuthError(LookupUnavailable):
    """The credential itself was rejected (invalid/expired token).

    Split out from LookupUnavailable so a caller CAN distinguish "my
    network is down right now" from "this key needs to be replaced" if it
    wants to -- but it is still a LookupUnavailable, because either way no
    answer was obtained and nothing may be cached as a miss.
    """


def _discogs_get(path: str, params: dict[str, str], api_key: str) -> dict:
    """GET against the Discogs API. Retries once on 429/503."""
    url = f"{_DISCOGS_BASE}/{path}?{urlencode(params)}"
    req = Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Authorization": f"Discogs token={api_key}",
        },
    )
    for attempt in range(2):
        try:
            _network_check(_DISCOGS_BASE)
            with urlopen(req, timeout=_TIMEOUT_S) as resp:
                data: dict = json.loads(resp.read().decode("utf-8"))
                return data
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                # Discogs's own wording for a bad/expired token is
                # "Invalid consumer token. Please register an app before
                # making requests." -- reported here rather than retried,
                # since retrying a bad credential wastes the retry budget
                # on a failure that will not change.
                raise DiscogsAuthError(
                    f"Discogs rejected the API key (HTTP 401): {exc}"
                ) from exc
            if exc.code in (429, 503) and attempt == 0:
                logger.warning(
                    "[discogs] rate-limited (HTTP %d), backing off %ds",
                    exc.code,
                    _RETRY_WAIT_S,
                )
                import time as _time

                _time.sleep(_RETRY_WAIT_S)
                continue
            raise LookupUnavailable(str(exc)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LookupUnavailable(str(exc)) from exc
    raise LookupUnavailable("exhausted retries")  # unreachable but explicit


def search_artist(name: str, api_key: str) -> tuple[str, str] | None:
    """Search Discogs for an artist by name.

    Returns (discogs_id, canonical_name), or None when Discogs answered
    and had no exact-name match. Raises LookupUnavailable when no answer
    was obtained at all -- see the module docstring's three-state
    contract; never conflate the two.
    """
    try:
        data = _discogs_get(
            "database/search",
            {"q": name, "type": "artist"},
            api_key,
        )
    except LookupUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 -- must not escape as an
        # unclassified error; every failure here is one of the two states
        # the caller is contractually allowed to see.
        raise LookupUnavailable(str(exc)) from exc

    results = data.get("results", [])
    query_lower = name.strip().lower()
    for result in results:
        title = (result.get("title") or "").strip()
        # Exact match only, not "contains" -- Discogs search results carry
        # no confidence score comparable to MusicBrainz's, so the same
        # false-positive risk mb_enrich._same_artist guards against
        # ("Red" -> Red Hot Chili Peppers) applies here without a score to
        # lean on. Exact (case-insensitive) is the conservative choice.
        if title.lower() == query_lower:
            artist_id = result.get("id")
            if artist_id is not None:
                return str(artist_id), title
    return None
