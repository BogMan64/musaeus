#!/usr/bin/env python3
"""
MUSAEUS — Stage: MusicBrainz
Lookup ISRC and MusicBrainz Recording IDs for tracks that don't have them yet.

What it does:
  - Finds CATALOGUED archive rows where artist AND title are non-empty
  - Queries the MusicBrainz JSON API for recording matches
  - Extracts ISRC (from isrcs array) and MBID (recording id)
  - Stores results in archive columns: mbid TEXT, isrc TEXT
  - Rate limits to 1 request per second (MusicBrainz requirement)
  - Skips tracks that already have mbid set (idempotent)
  - Handles HTTP errors gracefully (log and continue)

Requirements:
  - Network access to musicbrainz.org
  - No third-party deps (uses urllib.request)
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from urllib.parse import quote

from ..config import get_config
from ..context import RunContext, StageResult
from ..db import log_event
from .base import BaseStage

logger = logging.getLogger(__name__)

_MB_BASE_URL = "https://musicbrainz.org/ws/2"
_USER_AGENT = "Musaeus/1.0 (https://github.com/musaeus/musaeus)"
_RATE_LIMIT_DELAY = 1.1  # MusicBrainz requires max 1 req/s
_TIMEOUT_S = 15
_COMMIT_EVERY = 25


def _mb_search_recording(artist: str, title: str) -> dict | None:
    """
    Search MusicBrainz for a recording matching artist + title.
    Returns dict with keys 'mbid' and 'isrc' (both may be None).
    Returns None on network/parse failure.
    """
    query = f'artist:"{quote(artist)}" AND recording:"{quote(title)}"'
    url = f"{_MB_BASE_URL}/recording?query={quote(query)}&fmt=json&limit=3"

    req = urllib.request.Request(url)
    req.add_header("User-Agent", _USER_AGENT)
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as exc:
        logger.warning("MusicBrainz API error for %r - %r: %s", artist, title, exc)
        return None

    recordings = data.get("recordings", [])
    if not recordings:
        return None

    # Take the first (best-scored) result
    rec = recordings[0]
    mbid = rec.get("id")
    isrc = None

    # Extract first ISRC if available
    isrcs = rec.get("isrcs", [])
    if isrcs and isinstance(isrcs, list):
        isrc = isrcs[0]

    return {"mbid": mbid, "isrc": isrc}


class MusicBrainzStage(BaseStage):
    """
    MusicBrainz — lookup ISRC and MBID for catalogued tracks.
    """

    NAME = "musicbrainz"

    # ── Validate ──────────────────────────────────────────────────────────────

    def validate(self, ctx: RunContext) -> None:
        count = ctx.conn.execute(
            """
            SELECT COUNT(*) FROM archive
            WHERE status = 'CATALOGUED'
              AND artist IS NOT NULL AND TRIM(artist) != ''
              AND title IS NOT NULL AND TRIM(title) != ''
              AND (mbid IS NULL OR TRIM(mbid) = '')
            """
        ).fetchone()[0]
        logger.info("[musicbrainz] %d track(s) eligible for MusicBrainz lookup", count)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_pending(self, ctx: RunContext) -> list[dict]:
        """Return rows needing MusicBrainz lookup."""
        rows = ctx.conn.execute(
            """
            SELECT file_path, artist, title
            FROM archive
            WHERE status = 'CATALOGUED'
              AND artist IS NOT NULL AND TRIM(artist) != ''
              AND title IS NOT NULL AND TRIM(title) != ''
              AND (mbid IS NULL OR TRIM(mbid) = '')
            ORDER BY artist, album, title
            """
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)

        pending = self._get_pending(ctx)
        total = len(pending)
        result.notes.append(f"tracks to lookup: {total}")

        if not total:
            result.notes.append("nothing to do — all eligible tracks already have mbid")
            ctx.record_stage(result)
            return result

        enriched = 0
        skipped_no_match = 0
        api_errors = 0

        for i, row in enumerate(pending, 1):
            result.files_processed += 1
            artist = row["artist"].strip()
            title = row["title"].strip()

            time.sleep(_RATE_LIMIT_DELAY)

            mb_result = _mb_search_recording(artist, title)

            if mb_result is None:
                api_errors += 1
                result.files_errored += 1
                continue

            mbid = mb_result.get("mbid")
            isrc = mb_result.get("isrc")

            if not mbid:
                skipped_no_match += 1
                result.files_skipped += 1
                continue

            # Update archive
            ctx.conn.execute(
                "UPDATE archive SET mbid = ?, isrc = ? WHERE file_path = ?",
                (mbid, isrc, row["file_path"]),
            )

            # Log event
            log_event(
                ctx.conn,
                run_id=ctx.run_id,
                event_type="MB_ENRICHED",
                file_path=row["file_path"],
                new_value=json.dumps({"mbid": mbid, "isrc": isrc}),
                stage=self.NAME,
                note=f"artist={artist!r} title={title!r}",
            )

            enriched += 1
            result.files_changed += 1
            logger.info(
                "MB enriched: %s — %s → mbid=%s isrc=%s",
                row["file_path"], title, mbid, isrc,
            )

            if i % _COMMIT_EVERY == 0:
                ctx.conn.commit()
                logger.info("[musicbrainz] checkpoint %d/%d", i, total)

        ctx.conn.commit()

        result.notes.append(f"enriched: {enriched}")
        if skipped_no_match:
            result.notes.append(f"no match found: {skipped_no_match}")
        if api_errors:
            result.notes.append(f"API errors (skipped): {api_errors}")

        ctx.record_stage(result)
        return result

    # ── Dry run ───────────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=True)

        pending = self._get_pending(ctx)
        total = len(pending)

        result.files_processed = total
        result.notes.append(f"[DRY RUN] would lookup {total} track(s) on MusicBrainz")
        result.notes.append("  no API calls will be made, no DB changes")
        if total:
            result.notes.append(
                f"  estimated time: ~{total * _RATE_LIMIT_DELAY:.0f}s "
                f"(rate limited to 1 req/s)"
            )

        ctx.record_stage(result)
        return result
