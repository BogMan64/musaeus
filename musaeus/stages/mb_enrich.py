#!/usr/bin/env python3
"""
MUSAEUS — Stage: MBEnrich
MusicBrainz metadata enrichment for CATALOGUED archive rows.

What it does:
  - Finds CATALOGUED rows where mb_enriched_at IS NULL (not yet looked up).
    Deliberately NOT `mb_artist_id IS NULL`: that re-queries every track
    MusicBrainz could not identify, on every run, for ever.
  - Queries MusicBrainz search API for artist MBID and canonical name
  - Queries MusicBrainz release API for album MBID when artist is found
  - Writes back to archive: mb_artist_id, mb_artist_name, mb_release_id
    (columns auto-added on first run via _ensure_columns)
  - Caches per-artist results in memory (avoids repeat queries)
  - Rate-limits to 1 request/second (MusicBrainz free tier requirement)
  - Logs MB_ARTIST_FOUND / MB_ARTIST_NOT_FOUND / MB_RELEASE_FOUND per file
  - dry_run() reports what would be found without any DB changes

Requirements:
  - Network access to musicbrainz.org
  - No API key required (MusicBrainz is free/open)
  - User-Agent header sent per MB guidelines

Graceful degradation:
  - Network errors → artist skipped, run continues
  - Artist not found → mb_artist_id left NULL, run continues
  - MusicBrainz rate-limit (503) → backs off 5 s and retries once

ORPHEUS equivalent: SCRIPTS/orpheus_mb_enricher.py
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..context import RunContext, StageResult
from ..db import (
    mb_cache_get_artist,
    mb_cache_put_artist,
    open_mb_cache,
)
from ..network_policy import check as _network_check
from .base import BaseStage
from .enrich import _clean_artist_for_lookup

logger = logging.getLogger(__name__)

_MB_BASE = "https://musicbrainz.org/ws/2"
_USER_AGENT = "Musaeus/1.0 (music-library-manager; contact@musaeus.local)"
#: Distinguishes "never looked up" from "looked up, not found".
#: None is a real cached answer here, so it cannot double as the
#: absent marker -- doing so would re-query every negative result.
_MISSING = object()

_RATE_LIMIT_S = 1.1  # MB requires ≤ 1 req/s for unauthenticated access
_TIMEOUT_S = 15
_RETRY_WAIT_S = 5
_COMMIT_EVERY = 50
_ARTIST_SCORE = 85  # minimum MB score to accept an artist match (0-100)


# ── Column migration ──────────────────────────────────────────────────────────


def _ensure_columns(conn) -> None:  # type: ignore[type-arg]
    """Add MB columns to archive if they don't exist yet (auto-migrate)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(archive)").fetchall()}
    for col, typedef in (
        ("mb_artist_id", "TEXT"),
        ("mb_artist_name", "TEXT"),
        ("mb_release_id", "TEXT"),
        ("mb_enriched_at", "TEXT"),
    ):
        if col not in existing:
            conn.execute(f"ALTER TABLE archive ADD COLUMN {col} {typedef}")
    conn.commit()


# ── MB API helpers ────────────────────────────────────────────────────────────


def _mb_get(path: str, params: dict[str, str]) -> dict:
    """
    Perform a GET request to the MusicBrainz JSON API.
    Retries once on 503 (rate-limit).  Raises on other errors.
    """
    url = f"{_MB_BASE}/{path}?{urlencode({**params, 'fmt': 'json'})}"
    req = Request(url, headers={"User-Agent": _USER_AGENT})

    for attempt in range(2):
        try:
            # Ask the gateway before dispatching. Under LOCAL_ONLY this raises,
            # and the attempt is recorded BEFORE raising so the broad except
            # below cannot erase the evidence -- see network_policy.py.
            _network_check(_MB_BASE)
            with urlopen(req, timeout=_TIMEOUT_S) as resp:
                data: dict = json.loads(resp.read().decode("utf-8"))
                return data
        except urllib.error.HTTPError as exc:
            if exc.code == 503 and attempt == 0:
                logger.warning("[mb_enrich] rate-limited, backing off %ds", _RETRY_WAIT_S)
                time.sleep(_RETRY_WAIT_S)
                continue
            raise
    return {}  # unreachable but satisfies type checker


def _same_artist(ours: str, theirs: str) -> bool:
    """True when two artist names denote the same act.

    Folded through the article transform first, so the library's storage
    form matches the natural form MusicBrainz returns ("Beatles, The" ==
    "The Beatles"), then reduced to alphanumerics so punctuation and case
    differences survive ("R.e.m" == "R.E.M.", "a-ha" == "a\u2010ha").
    Anything beyond that is a different artist.
    """

    def fold(name: str) -> str:
        natural = _clean_artist_for_lookup((name or "").strip())
        # "&" and "and" are the same word in a band name, and the two forms
        # are used interchangeably by tag sources and by MusicBrainz. Without
        # this, stripping punctuation turns "Hall & Oates" into "halloates"
        # and "Hall and Oates" into "hallandoates", and the guard rejects a
        # correct match -- the mirror-image of the bug it was added to fix.
        # Same for "+", which appears in credits like "Black Eyed Peas +
        # Shakira".
        natural = re.sub(r"\s*[&+]\s*", " and ", natural.lower())
        return re.sub(r"[^0-9a-z]+", "", natural)

    return bool(fold(ours)) and fold(ours) == fold(theirs)


class LookupUnavailable(Exception):
    """MusicBrainz gave no answer: timeout, 503, DNS, or a policy refusal.

    Distinct from "MusicBrainz answered, and has no such artist". Both used
    to arrive here as None, and the caller stamped mb_enriched_at on the
    strength of it -- so a network wobble permanently recorded a row as
    looked-up, and a poisoned entry went into the persistent cache on top.
    Measured in the 16:16 run on 2026-08-25: 32 transport failures against
    3 successes. Under the marker written earlier that day, all 32 would
    have been marked done and never asked again.

    Three states, and the schema has to carry all three:
        (mbid, name)        -- found
        None                -- asked, definitively not found  -> stamp
        LookupUnavailable   -- never asked successfully       -> leave alone
    """


def _search_artist(artist_name: str) -> tuple[str, str] | None:
    """
    Search MusicBrainz for an artist by name.

    Returns (mbid, canonical_name), or None when MusicBrainz answered and
    had no match good enough. Raises LookupUnavailable when no answer was
    obtained at all -- never conflate the two.
    """
    try:
        data = _mb_get(
            "artist",
            # NOT quote()d -- _mb_get's urlencode() encodes the whole query
            # once. Pre-encoding here sent "Dusty%20Springfield" as a literal
            # Lucene term, so every artist name containing a space matched
            # nothing at all, silently, for as long as this file existed.
            # Verified live 2026-08-23: single-word names ("Abba", "Cher")
            # resolved; "Dusty Springfield" returned None, and did so again
            # the moment the encoding was removed -- as a match.
            # Searched in MusicBrainz's natural form, not the library's
            # storage form. MUSAEUS files "The Stooges" as "Stooges, The";
            # MusicBrainz has never heard of that, so the query returned no
            # match and the row was cached as "no such artist" -- FOREVER.
            #
            # Measured on mb_cache.db 2026-08-29: 376 of 839 cached misses
            # (45%) are in `X, The` form, and **0 of 2,158 successes are**.
            # Not one article-suffix lookup has ever succeeded. Confirmed
            # live the same day: 'Stooges, The' -> no match, 'The Stooges'
            # -> 794c6bf2. Same for The Crickets and The Pogues.
            #
            # `_clean_artist_for_lookup` already existed and already did
            # this correctly -- `_same_artist` has been folding through it
            # to ACCEPT results all along. It was simply never applied to
            # the query it was written for.
            {"query": f'artist:"{_clean_artist_for_lookup(artist_name)}"', "limit": "3"},
        )
    except Exception as exc:
        # Was `return None`, which the caller could not tell apart from a
        # genuine miss.
        logger.warning("[mb_enrich] artist search error for %r: %s", artist_name, exc)
        raise LookupUnavailable(str(exc)) from exc

    artists = data.get("artists", [])
    for artist in artists:
        score = int(artist.get("score", 0))
        if score < _ARTIST_SCORE:
            continue
        # Score alone is not identity. MusicBrainz will score a containing
        # name at 100 -- measured 2026-08-23 against the live vault, where
        # this wrote 27 wrong MBIDs before it was caught:
        #     "Red"            -> Red Hot Chili Peppers
        #     "Little Feat"    -> Little Richard
        #     "Dion"           -> Celine Dion
        #     "Jan & Dean"     -> Jan Arnald
        # Each was then stamped mb_enriched_at, so it would never have been
        # revisited. The name has to actually agree.
        if _same_artist(artist_name, artist.get("name", "")):
            return artist["id"], artist["name"]
        logger.debug(
            "[mb_enrich] rejecting %r for %r (score %d, different artist)",
            artist.get("name"),
            artist_name,
            score,
        )

    return None


def _search_release(artist_mbid: str, album_name: str) -> str | None:
    """
    Search MusicBrainz for a release by artist MBID + album title.
    Returns release MBID or None.
    """
    if not album_name:
        return None
    try:
        data = _mb_get(
            "release",
            {
                # Not quote()d, for the same reason as _search_artist above.
                "query": f'artist:"{artist_mbid}" AND release:"{album_name}"',
                "limit": "1",
            },
        )
    except Exception as exc:
        logger.warning(
            "[mb_enrich] release search error for mbid=%s album=%r: %s",
            artist_mbid,
            album_name,
            exc,
        )
        return None

    releases = data.get("releases", [])
    if releases:
        score = int(releases[0].get("score", 0))
        if score >= 70:
            release_id: str = releases[0]["id"]
            return release_id
    return None


# ── Stage ─────────────────────────────────────────────────────────────────────


class MBEnrichStage(BaseStage):
    """
    MBEnrich — MusicBrainz artist + release MBID lookup for CATALOGUED files.
    """

    NAME = "mb_enrich"

    # ── Validate ──────────────────────────────────────────────────────────────

    def validate(self, ctx: RunContext) -> None:
        # Connectivity is no longer checked here as a hard failure (2026-08-17):
        # this stage joined DEFAULT_PIPELINE's default-on chain, and a network
        # hiccup must never block or fail the whole run -- matches EnrichStage's
        # existing graceful-degradation pattern (missing API key -> warn +
        # no-op, not a StageError). The actual connectivity check now lives in
        # _enrich() itself, where a real StageResult can be returned instead of
        # raising. See _enrich()'s early-return block below.

        # Count work to do (columns may not exist yet — use try/except)
        try:
            count = ctx.conn.execute(
                "SELECT COUNT(*) FROM archive "
                "WHERE status='CATALOGUED' AND mb_enriched_at IS NULL "
                "AND artist IS NOT NULL AND trim(artist) != ''"
            ).fetchone()[0]
        except Exception:
            count = ctx.conn.execute(
                "SELECT COUNT(*) FROM archive "
                "WHERE status='CATALOGUED' "
                "AND artist IS NOT NULL AND trim(artist) != ''"
            ).fetchone()[0]

        logger.info("[mb_enrich] %d track(s) need MusicBrainz enrichment", count)

    # ── Shared logic ──────────────────────────────────────────────────────────

    def _enrich(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        # Stamping a not-found row would otherwise be a one-way door: an
        # artist MusicBrainz cannot identify today stays unqueried for
        # ever, even after canon work corrects the name that failed. Same
        # escape hatch the other resumable stages carry.
        force: bool = bool(ctx.get("mb_enrich_force", False))
        if force:
            result.notes.append("force: re-querying rows already looked up")

        # Graceful degradation (2026-08-17, matches EnrichStage's missing-
        # API-key pattern): a single lightweight connectivity probe before
        # doing any real work. Unreachable -> skip and report, never fail
        # the stage/run over it. Real per-request failures further down are
        # already caught individually (_search_artist/_search_release) and
        # skip that one artist/release without aborting the rest.
        try:
            req = Request(
                "https://musicbrainz.org/",
                headers={"User-Agent": _USER_AGENT},
                method="HEAD",
            )
            urlopen(req, timeout=5)
        except Exception as exc:
            result.notes.append(f"MusicBrainz not reachable — skipping mb_enrich this run. ({exc})")
            ctx.record_stage(result)
            return result

        if not dry_run:
            _ensure_columns(ctx.conn)

        # Fetch rows that need enrichment
        try:
            rows = ctx.conn.execute(
                """
                SELECT file_path, artist, album
                FROM archive
                WHERE status = 'CATALOGUED'
                  AND (mb_enriched_at IS NULL OR :force)
                  AND artist IS NOT NULL AND trim(artist) != ''
                ORDER BY artist, album
                """,
                {"force": 1 if force else 0},
            ).fetchall()
        except Exception:
            # mb_artist_id column doesn't exist yet (dry-run before first real run)
            rows = ctx.conn.execute(
                """
                SELECT file_path, artist, album
                FROM archive
                WHERE status = 'CATALOGUED'
                  AND artist IS NOT NULL AND trim(artist) != ''
                ORDER BY artist, album
                """
            ).fetchall()

        # Persistent across runs. The dicts below stay as the in-run layer;
        # this is what stops every batch re-asking MusicBrainz about the
        # same artists. musaeus.db is wiped between batches, so the cache
        # lives beside the hash index instead.
        try:
            mb_cache = open_mb_cache(ctx.config.mb_cache_path)
        except Exception as exc:  # a cache failure must not stop enrichment
            logger.warning("[mb_enrich] persistent cache unavailable: %s", exc)
            mb_cache = None

        # Per-artist cache: artist_lower → (mbid, mb_name) | None
        artist_cache: dict[str, tuple[str, str] | None] = {}
        # Per-artist+album cache: (artist_mbid, album_lower) → release_mbid | None
        release_cache: dict[tuple[str, str], str | None] = {}

        cache_hits = 0
        unavailable = 0
        found_artists = 0
        found_releases = 0
        not_found = 0
        would_query_artists: set[str] = set()

        from datetime import datetime, timezone

        now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

        for row in rows:
            result.files_processed += 1
            fp = row["file_path"]
            artist = (row["artist"] or "").strip()
            album = (row["album"] or "").strip()
            artist_lower = artist.lower()

            # ── Artist lookup ──────────────────────────────────────────────
            if artist_lower not in artist_cache:
                if dry_run:
                    # FIXED 2026-08-18: dry_run must not make the real
                    # network call at all (previously only the DB write
                    # was gated). Not populating artist_cache here is
                    # deliberate -- the release lookup below is naturally
                    # never reached for an uncached artist, since this
                    # branch always continues past it.
                    would_query_artists.add(artist_lower)
                    result.files_skipped += 1
                    continue
                cached = _MISSING
                if mb_cache is not None:
                    try:
                        cached = mb_cache_get_artist(mb_cache, artist_lower)
                    except KeyError:
                        cached = _MISSING
                    except Exception as exc:
                        logger.debug("[mb_enrich] cache read failed: %s", exc)
                if cached is not _MISSING:
                    artist_cache[artist_lower] = cached
                    cache_hits += 1
                else:
                    time.sleep(_RATE_LIMIT_S)
                    try:
                        match = _search_artist(artist)
                    except LookupUnavailable as exc:
                        # No answer, so nothing is known and nothing may be
                        # recorded: not the marker, and above all not the
                        # persistent cache, which would carry the mistake
                        # into every future run. The row keeps
                        # mb_enriched_at NULL and is retried next batch --
                        # the one case where re-querying IS the right call.
                        unavailable += 1
                        result.files_skipped += 1
                        logger.warning(
                            "[mb_enrich] no answer for %r (%s) -- left for a later run",
                            artist,
                            exc,
                        )
                        continue
                    artist_cache[artist_lower] = match
                    if mb_cache is not None:
                        try:
                            mb_cache_put_artist(mb_cache, artist_lower, match)
                        except Exception as exc:
                            logger.debug("[mb_enrich] cache write failed: %s", exc)
                    # Logged here rather than after the cache branch: `match`
                    # is only bound on this path, and reading it after a
                    # cache HIT used whatever the previous iteration left in
                    # it -- or raised NameError on the very first row.
                    if match:
                        logger.info(
                            "[mb_enrich] artist found: %r → %r  mbid=%s",
                            artist,
                            match[1],
                            match[0],
                        )
                    else:
                        logger.debug("[mb_enrich] artist not found: %r", artist)

            artist_match = artist_cache[artist_lower]

            if artist_match is None:
                not_found += 1
                result.files_skipped += 1
                if not dry_run:
                    # Stamp the row even though nothing was found. The
                    # marker records that the LOOKUP HAPPENED, not that it
                    # succeeded -- those are different facts, and only the
                    # first is a reason not to repeat the work.
                    #
                    # Without this, a track MusicBrainz cannot identify
                    # keeps every mb_ column NULL and is re-queried on every
                    # run for ever, at a rate-limited second plus 503
                    # backoffs each. On the live vault that is 2,328 of
                    # 10,873 rows, asked again every single batch.
                    ctx.conn.execute(
                        "UPDATE archive SET mb_enriched_at=? WHERE file_path=?",
                        (now, fp),
                    )
                    ctx.log_event(
                        "MB_ARTIST_NOT_FOUND",
                        file_path=fp,
                        stage=self.NAME,
                        note=f"artist={artist!r}",
                    )
                continue

            mb_artist_id, mb_artist_name = artist_match
            found_artists += 1

            # ── Release lookup ─────────────────────────────────────────────
            release_key = (mb_artist_id, album.lower())
            if album and release_key not in release_cache:
                time.sleep(_RATE_LIMIT_S)
                mb_release_id = _search_release(mb_artist_id, album)
                release_cache[release_key] = mb_release_id
                if mb_release_id:
                    logger.info("[mb_enrich] release found: %r → %s", album, mb_release_id)
            mb_release_id = release_cache.get(release_key) if album else None

            if mb_release_id:
                found_releases += 1

            result.files_changed += 1

            if not dry_run:
                ctx.conn.execute(
                    """
                    UPDATE archive
                       SET mb_artist_id=?,
                           mb_artist_name=?,
                           mb_release_id=?,
                           mb_enriched_at=?
                     WHERE file_path=?
                    """,
                    (mb_artist_id, mb_artist_name, mb_release_id, now, fp),
                )
                ctx.log_event(
                    "MB_ARTIST_FOUND",
                    file_path=fp,
                    new_value=mb_artist_id,
                    stage=self.NAME,
                    note=f"name={mb_artist_name!r} release={mb_release_id}",
                )

            if result.files_processed % _COMMIT_EVERY == 0 and not dry_run:
                ctx.conn.commit()
                logger.info("[mb_enrich] checkpoint %d", result.files_processed)

        prefix = "Would enrich" if dry_run else "Enriched"
        result.notes.append(f"{prefix} {found_artists} artist(s) with MusicBrainz MBID.")
        if found_releases:
            result.notes.append(f"  {found_releases} release MBID(s) found.")
        if not_found:
            result.notes.append(f"  {not_found} artist(s) not found on MusicBrainz.")
        if unavailable:
            # Deferred, not decided. Said plainly because the old code
            # reported errors=0 here while marking these rows done.
            result.notes.append(
                f"  {unavailable} artist(s) got NO ANSWER (network/timeout/503) — "
                f"not marked, will be retried on the next run."
            )
        if would_query_artists:
            result.notes.append(
                f"  {len(would_query_artists)} artist(s) would be queried via "
                f"MusicBrainz in a real run — not looked up now, dry-run makes "
                f"no network calls."
            )

        ctx.record_stage(result)
        return result

    # ── Dry run / Run ─────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._enrich(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._enrich(ctx, dry_run=False)
