#!/usr/bin/env python3
"""
MUSAEUS — Stage: MBEnrich
MusicBrainz metadata enrichment for CATALOGUED archive rows.

What it does:
  - Finds CATALOGUED rows where mb_artist_id IS NULL (not yet enriched)
  - Queries MusicBrainz search API for artist MBID and canonical name
  - Queries MusicBrainz release API for album MBID when artist is found
  - Writes back to archive: mb_artist_id, mb_artist_name, mb_release_id
    (columns declared in db.py's _MIGRATIONS, applied by open_db())
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
import time
import urllib.error
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from ..context import RunContext, StageResult
from .base import BaseStage

logger = logging.getLogger(__name__)

_MB_BASE = "https://musicbrainz.org/ws/2"
_USER_AGENT = "Musaeus/1.0 (music-library-manager; contact@musaeus.local)"
_RATE_LIMIT_S = 1.1  # MB requires ≤ 1 req/s for unauthenticated access
_TIMEOUT_S = 15
_RETRY_WAIT_S = 5
_COMMIT_EVERY = 50
_ARTIST_SCORE = 85  # minimum MB score to accept an artist match (0-100)


# ── Column migration ──────────────────────────────────────────────────────────


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


def _search_artist(artist_name: str) -> tuple[str, str] | None:
    """
    Search MusicBrainz for an artist by name.
    Returns (mbid, canonical_name) or None if not found / score too low.
    """
    try:
        data = _mb_get(
            "artist",
            {"query": f'artist:"{quote(artist_name)}"', "limit": "3"},
        )
    except Exception as exc:
        logger.warning("[mb_enrich] artist search error for %r: %s", artist_name, exc)
        return None

    artists = data.get("artists", [])
    for artist in artists:
        score = int(artist.get("score", 0))
        if score >= _ARTIST_SCORE:
            return artist["id"], artist["name"]

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
                "query": f'artist:"{artist_mbid}" AND release:"{quote(album_name)}"',
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
        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive "
            "WHERE status='CATALOGUED' AND mb_artist_id IS NULL "
            "AND artist IS NOT NULL AND trim(artist) != ''"
        ).fetchone()[0]
        logger.info("[mb_enrich] %d track(s) need MusicBrainz enrichment", count)

    # ── Shared logic ──────────────────────────────────────────────────────────

    def _enrich(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)

        # Graceful degradation (2026-08-17, matches EnrichStage's missing-
        # API-key pattern): a single lightweight connectivity probe before
        # doing any real work. Unreachable -> skip and report, never fail
        # the stage/run over it. Real per-request failures further down are
        # already caught individually (_search_artist/_search_release) and
        # skip that one artist/release without aborting the rest.
        # ...but not under dry_run. The probe is a real outbound request, and
        # it sat ahead of every dry_run check in this method -- so the
        # 2026-08-18 fix that stopped previews from making per-artist lookups
        # still left a preview making this one. Under a preview there is also
        # nothing to degrade gracefully *to*: no lookup is going to happen
        # either way, so reachability cannot change what is reported.
        if dry_run:
            result.notes.append(
                "[DRY RUN] skipping MusicBrainz connectivity probe — a preview makes no requests"
            )
        else:
            try:
                req = Request(
                    "https://musicbrainz.org/",
                    headers={"User-Agent": _USER_AGENT},
                    method="HEAD",
                )
                urlopen(req, timeout=5)
            except Exception as exc:
                result.notes.append(
                    f"MusicBrainz not reachable — skipping mb_enrich this run. ({exc})"
                )
                ctx.record_stage(result)
                return result

        # Fetch rows that need enrichment
        rows = ctx.conn.execute(
            """
            SELECT file_path, artist, album
            FROM archive
            WHERE status = 'CATALOGUED'
              AND mb_artist_id IS NULL
              AND artist IS NOT NULL AND trim(artist) != ''
            ORDER BY artist, album
            """
        ).fetchall()

        # Per-artist cache: artist_lower → (mbid, mb_name) | None
        artist_cache: dict[str, tuple[str, str] | None] = {}
        # Per-artist+album cache: (artist_mbid, album_lower) → release_mbid | None
        release_cache: dict[tuple[str, str], str | None] = {}

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
                time.sleep(_RATE_LIMIT_S)
                match = _search_artist(artist)
                artist_cache[artist_lower] = match
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
