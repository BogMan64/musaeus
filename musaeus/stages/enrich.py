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
from ..context import RunContext, StageResult
from .base import BaseStage

logger = logging.getLogger(__name__)

_LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
_RATE_LIMIT_DELAY = 0.2  # 5 req/s
_TIMEOUT_S = 10
_COMMIT_EVERY = 50

# Article suffixes stored in DB as  "Beatles, The"  →  " (the)"  bracket form.
# Strip these before querying Last.fm, which expects plain "The Beatles".
#
# Imported from normalize.py rather than redeclared here. They were
# previously duplicated -- byte-identical copies in both modules -- and
# duplicated article handling is exactly what let the article-suffix bug
# regress three separate times (scope doc §5). One definition, one place to
# fix. normalize.py owns them because it owns the canonical storage form.
from .normalize import _ARTICLE_COMMA_RE, _ARTICLE_SUFFIX_RE  # noqa: E402


def _clean_artist_for_lookup(artist: str) -> str:
    """
    Convert stored article-suffix form to natural form for Last.fm.

    Examples
    --------
    "Beatles, The (the)"  → "The Beatles"
    "Rolling Stones ( the )" → "The Rolling Stones"
    "Beatles, The"        → "The Beatles"
    "Cranberries, The"    → "The Cranberries"
    "Refused"             → "Refused"  (unchanged)
    """
    name = artist.strip()

    # Strip the parenthesised article suffix first.
    m = _ARTICLE_SUFFIX_RE.search(name)
    if m:
        article = m.group(1).strip().capitalize()
        base = name[: m.start()].strip().rstrip(",").strip()
        # The base can still carry the comma-article form -- the documented
        # double spelling "Beatles, The (the)". Without this, the paren was
        # consumed and the comma suffix left behind, producing
        # "The Beatles, The" and sending a name to Last.fm that matches
        # nothing, so those artists silently never got enriched. Same bug
        # class as the one fixed in normalize.py's own suffix handling.
        m_inner = _ARTICLE_COMMA_RE.search(base)
        if m_inner:
            base = base[: m_inner.start()].strip()
        return f"{article} {base}" if base else name

    # Comma-article suffix without parens: "Beatles, The"
    m2 = _ARTICLE_COMMA_RE.search(name)
    if m2:
        article = m2.group(1).strip().capitalize()
        base = name[: m2.start()].strip()
        return f"{article} {base}" if base else name

    return name


def _lastfm_top_tags(artist: str, api_key: str, limit: int = 5) -> list[str]:
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
        cfg = ctx.config
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

        cfg = ctx.config
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

        # Pre-build a library cross-reference: if other tracks by the same artist
        # already have a genre set, use that immediately (no API call needed).
        # This is instant, free, and avoids the 5 req/s Last.fm rate limit.
        artist_genre_from_library: dict[str, str] = {}
        lib_rows = ctx.conn.execute(
            """
            SELECT LOWER(TRIM(artist)) AS artist_key, genre, COUNT(*) AS cnt
            FROM archive
            WHERE genre IS NOT NULL AND TRIM(genre) != ''
              AND artist IS NOT NULL AND TRIM(artist) != ''
            GROUP BY artist_key, genre
            ORDER BY cnt DESC
            """
        ).fetchall()
        for lr in lib_rows:
            akey = lr["artist_key"]
            if akey not in artist_genre_from_library:
                artist_genre_from_library[akey] = lr["genre"]

        # Per-artist cache: artist_lower → resolved genre or None
        artist_cache: dict[str, str | None] = {}
        enriched = 0
        skipped_no_tag = 0
        skipped_api_err = 0
        enriched_from_library = 0
        would_query_artists: set[str] = set()

        for row in rows:
            result.files_processed += 1
            artist = row["artist"].strip()
            artist_lower = artist.lower()

            # Library cross-reference: check if other tracks by this artist
            # already have a genre assigned (instant, no API call needed)
            if artist_lower in artist_genre_from_library and artist_lower not in artist_cache:
                artist_cache[artist_lower] = artist_genre_from_library[artist_lower]
                enriched_from_library += 1
                logger.debug("genre from library: %r → %r", artist, artist_cache[artist_lower])

            # Cache hit
            if artist_lower in artist_cache:
                resolved = artist_cache[artist_lower]
            elif dry_run:
                # FIXED 2026-08-18: dry_run must not make the real network
                # call at all (previously only the DB write was gated,
                # so "--dry-run" still hit Last.fm for real). Anything not
                # already resolvable from library data is reported as
                # "would query", not actually looked up.
                would_query_artists.add(artist_lower)
                result.files_skipped += 1
                continue
            else:
                lookup_name = _clean_artist_for_lookup(artist)
                if lookup_name != artist:
                    logger.debug("article-strip: %r → %r for Last.fm lookup", artist, lookup_name)
                time.sleep(_RATE_LIMIT_DELAY)
                try:
                    tags = _lastfm_top_tags(lookup_name, api_key)
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
                logger.debug("Last.fm %r → tags=%r → resolved=%r", artist, tags[:3], resolved)

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
        if enriched_from_library:
            result.notes.append(
                f"{enriched_from_library} artist(s) resolved from existing library data (no API call)."
            )
        if would_query_artists:
            result.notes.append(
                f"{len(would_query_artists)} artist(s) would be queried via Last.fm in a "
                f"real run — not looked up now, dry-run makes no network calls."
            )
        if skipped_no_tag:
            result.notes.append(f"{skipped_no_tag} artist(s) had no resolvable Last.fm tag.")
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
