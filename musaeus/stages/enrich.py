#!/usr/bin/env python3
"""
MUSAEUS — Stage: Enrich
Last.fm genre enrichment for tracks with missing or unresolved genres.

What it does:
  - Finds CATALOGUED archive rows where genre is NULL or empty
  - Looks up artist on Last.fm API → top tag(s)
  - Resolves tag against GenreCanon (allowed list + map + fuzzy ≥82)
  - Writes resolved genre back to archive row
  - Logs GENRE_ENRICHED event per file updated
  - Caches per-artist results in memory to avoid redundant API calls
  - Rate-limits to 5 requests/second (Last.fm free tier)
  - dry_run() shows what would be written without any DB changes

Requirements:
  - LASTFM_API_KEY in ~/.config/musaeus/settings.env
  - Network access to ws.audioscrobbler.com

Graceful degradation:
  - Missing API key → stage is skipped with a warning (no hard failure)
  - Network errors → individual artist is skipped, run continues
  - Unresolvable tag → genre left NULL, not forced
"""

from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from ..canon import GenreCanon
from ..config import get_config
from ..context import RunContext, StageResult
from .base import BaseStage

logger = logging.getLogger(__name__)

_LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
_RATE_LIMIT_DELAY = 0.2   # 5 req/s
_TIMEOUT_S       = 10
_COMMIT_EVERY    = 50


def _lastfm_top_tags(
    artist: str, api_key: str, limit: int = 5
) -> list[str]:
    """
    Query Last.fm artist.getTopTags.
    Returns a list of tag names (lowercase), most popular first.
    Raises on network/parse failure.
    """
    params = {
        "method": "artist.getTopTags",
        "artist": artist,
        "api_key": api_key,
        "format": "json",
        "limit": str(limit),
    }
    url = _LASTFM_URL + "?" + urlencode(params)
    with urlopen(url, timeout=_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    if "error" in data:
        raise ValueError(f"Last.fm error {data['error']}: {data.get('message', '')}")

    tags_obj = data.get("toptags", {})
    tags = tags_obj.get("tag", [])
    if isinstance(tags, dict):
        tags = [tags]
    return [t["name"].lower() for t in tags if isinstance(t, dict) and t.get("name")]


class EnrichStage(BaseStage):
    """
    Enrich — fill missing genres via Last.fm + GenreCanon resolution.
    """

    NAME = "enrich"

    # ── Validate ──────────────────────────────────────────────────────────────

    def validate(self, ctx: RunContext) -> None:
        cfg = get_config()
        if not cfg.lastfm_api_key:
            logger.warning(
                "[enrich] LASTFM_API_KEY not set — stage will be a no-op. "
                "Add it to ~/.config/musaeus/settings.env"
            )
        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive "
            "WHERE status='CATALOGUED' AND (genre IS NULL OR trim(genre)='')"
        ).fetchone()[0]
        logger.info("[enrich] %d track(s) need genre enrichment", count)

    # ── Shared logic ──────────────────────────────────────────────────────────

    def _enrich(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)

        cfg = get_config()
        api_key = cfg.lastfm_api_key
        if not api_key:
            result.notes.append(
                "LASTFM_API_KEY not set — skipping enrichment. "
                "Set it in ~/.config/musaeus/settings.env"
            )
            ctx.record_stage(result)
            return result

        genre_canon = GenreCanon(
            cfg.meta_dir / "genre_allowed.txt",
            cfg.meta_dir / "genre_map.tsv",
        )

        rows = ctx.conn.execute(
            """
            SELECT file_path, artist, title
            FROM archive
            WHERE status='CATALOGUED'
              AND (genre IS NULL OR trim(genre) = '')
              AND artist IS NOT NULL AND trim(artist) != ''
            ORDER BY artist, title
            """
        ).fetchall()

        # Per-artist cache: artist_lower → resolved genre or None
        artist_cache: dict[str, str | None] = {}
        enriched = 0
        skipped_no_tag = 0
        skipped_api_err = 0

        for row in rows:
            result.files_processed += 1
            artist = row["artist"].strip()
            artist_lower = artist.lower()

            # Cache hit
            if artist_lower in artist_cache:
                resolved = artist_cache[artist_lower]
            else:
                time.sleep(_RATE_LIMIT_DELAY)
                try:
                    tags = _lastfm_top_tags(artist, api_key)
                except Exception as exc:
                    logger.warning("Last.fm error for %r: %s", artist, exc)
                    artist_cache[artist_lower] = None
                    skipped_api_err += 1
                    result.files_skipped += 1
                    continue

                resolved = None
                for tag in tags:
                    r = genre_canon.resolve(tag)
                    if r:
                        resolved = r
                        break

                artist_cache[artist_lower] = resolved
                logger.debug(
                    "Last.fm %r → tags=%r → resolved=%r", artist, tags[:3], resolved
                )

            if resolved is None:
                skipped_no_tag += 1
                result.files_skipped += 1
                continue

            # Write it
            if not dry_run:
                ctx.conn.execute(
                    "UPDATE archive SET genre=? WHERE file_path=?",
                    (resolved, row["file_path"]),
                )
                ctx.log_event(
                    "GENRE_ENRICHED",
                    file_path=row["file_path"],
                    new_value=resolved,
                    stage=self.NAME,
                    note=f"artist={artist!r}",
                )

            result.files_changed += 1
            enriched += 1
            logger.info(
                "genre enriched: %s — %s → %s",
                row["file_path"],
                artist,
                resolved,
            )

            if result.files_processed % _COMMIT_EVERY == 0 and not dry_run:
                ctx.conn.commit()
                logger.info("[enrich] checkpoint %d", result.files_processed)

        prefix = "Would set" if dry_run else "Set"
        result.notes.append(f"{prefix} genre for {enriched} track(s).")
        if skipped_no_tag:
            result.notes.append(
                f"{skipped_no_tag} artist(s) had no resolvable Last.fm tag."
            )
        if skipped_api_err:
            result.notes.append(f"{skipped_api_err} Last.fm API error(s) — skipped.")

        ctx.record_stage(result)
        return result

    # ── Dry run ───────────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._enrich(ctx, dry_run=True)

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self, ctx: RunContext) -> StageResult:
        return self._enrich(ctx, dry_run=False)
