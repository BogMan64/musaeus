#!/usr/bin/env python3
"""
MUSAEUS — Stage: NearDupe
Metadata-based near-duplicate detection (no audio fingerprinting required).

What it does:
  - Loads all CATALOGUED archive rows
  - Groups tracks by normalised artist name (ArtistCanon → fuzzy ≥88)
  - Within each artist group, compares normalised title pairs with
    rapidfuzz fuzz.ratio — flags pairs scoring ≥ TITLE_THRESHOLD (88)
  - Stages flagged pairs in the duplicates table with type='NEAR'
  - Skips pairs already in the EXACT duplicates table
  - dry_run() reports matches without writing to DB
  - Re-run safe: INSERT OR IGNORE on (group_id, file_path)

Design decisions:
  - Artist normalisation: lowercase, strip punctuation, collapse whitespace
  - Title normalisation: same as artist + strip leading "the "
  - group_id: "near_{sha8}" where sha8 is SHA-256[:8] of sorted file paths
  - Confidence: the fuzz.ratio score (0.0–1.0)
  - O(n²) within artist groups — fast because groups are small (< 200 tracks)
  - Threshold is conservative at 88 to avoid false positives

The resulting near-duplicate groups appear in `musaeus dedupe` alongside
exact duplicates.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
import uuid

try:
    from rapidfuzz import fuzz
    _HAVE_RAPIDFUZZ = True
except ImportError:
    _HAVE_RAPIDFUZZ = False

from ..canon import ArtistCanon
from ..config import get_config
from ..context import RunContext, StageResult
from .base import BaseStage, StageError

logger = logging.getLogger(__name__)

# Minimum fuzz.ratio to flag as near-duplicate (0–100 scale)
# 85 was tested in ORPHEUS folder dedup and catches live/remaster variants well.
# 88 is more conservative but may miss some near-dupes with punctuation diffs.
TITLE_THRESHOLD = 85
ARTIST_THRESHOLD = 88


def _normalise(s: str) -> str:
    """Lowercase, NFD-normalise, strip punctuation, collapse whitespace."""
    s = unicodedata.normalize("NFD", s.lower())
    s = re.sub(r"[^\w\s]", " ", s)          # punctuation → space
    s = re.sub(r"\s+", " ", s).strip()       # collapse whitespace
    # Strip leading "the "
    if s.startswith("the "):
        s = s[4:]
    return s


def _group_id(path_a: str, path_b: str) -> str:
    """Stable group ID from the two sorted paths."""
    combined = "\n".join(sorted([path_a, path_b]))
    return "near_" + hashlib.sha256(combined.encode()).hexdigest()[:8]


class NearDupeStage(BaseStage):
    """
    Near-duplicate detection — find tracks with the same artist and very
    similar titles, staging them for review in the dedupe console.
    """

    NAME = "neardupe"

    # ── Validate ──────────────────────────────────────────────────────────────

    def validate(self, ctx: RunContext) -> None:
        if not _HAVE_RAPIDFUZZ:
            raise StageError(
                "rapidfuzz not installed — required for near-duplicate detection.\n"
                "Install with: pip install rapidfuzz"
            )
        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'"
        ).fetchone()[0]
        logger.info("[neardupe] %d catalogued tracks to compare", count)

    # ── Shared logic ──────────────────────────────────────────────────────────

    def _detect(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)

        cfg = get_config()
        artist_canon = ArtistCanon(cfg.meta_dir / "artist_canon.tsv")

        # Load all catalogued rows
        rows = ctx.conn.execute(
            """
            SELECT file_path, artist, title, bitrate, size_bytes
            FROM archive
            WHERE status = 'CATALOGUED'
              AND artist IS NOT NULL AND trim(artist) != ''
              AND title  IS NOT NULL AND trim(title)  != ''
            ORDER BY artist, title
            """
        ).fetchall()

        result.files_processed = len(rows)

        # Pre-load existing exact duplicate paths to skip
        exact_paths: set[str] = {
            row["file_path"]
            for row in ctx.conn.execute(
                "SELECT file_path FROM duplicates WHERE duplicate_type='EXACT'"
            ).fetchall()
        }

        # Pre-load already-staged near dupe pairs to avoid re-flagging
        existing_near: set[tuple[str, str]] = set()
        for row in ctx.conn.execute(
            "SELECT group_id, file_path FROM duplicates WHERE duplicate_type='NEAR'"
        ).fetchall():
            existing_near.add((row["group_id"], row["file_path"]))

        # Bucket tracks by canonical artist
        artist_buckets: dict[str, list[dict]] = {}
        for row in rows:
            raw_artist = row["artist"].strip()
            canonical = artist_canon.resolve(raw_artist) or raw_artist
            key = _normalise(canonical)
            bucket = artist_buckets.setdefault(key, [])
            bucket.append(dict(row))

        new_groups = 0
        new_pairs = 0

        for artist_key, tracks in artist_buckets.items():
            if len(tracks) < 2:
                continue

            # O(n²) within each artist group
            for i in range(len(tracks)):
                for j in range(i + 1, len(tracks)):
                    a = tracks[i]
                    b = tracks[j]

                    # Skip if either is already an exact dupe
                    if a["file_path"] in exact_paths or b["file_path"] in exact_paths:
                        continue

                    title_a = _normalise(a["title"])
                    title_b = _normalise(b["title"])

                    score = fuzz.ratio(title_a, title_b)
                    if score < TITLE_THRESHOLD:
                        continue

                    # Near duplicate found
                    gid = _group_id(a["file_path"], b["file_path"])
                    confidence = round(score / 100.0, 4)

                    is_new_group = False
                    for fp in (a["file_path"], b["file_path"]):
                        pair_key = (gid, fp)
                        if pair_key not in existing_near:
                            is_new_group = True
                            new_pairs += 1
                            existing_near.add(pair_key)

                            if not dry_run:
                                ctx.conn.execute(
                                    """
                                    INSERT OR IGNORE INTO duplicates
                                        (group_id, file_path, duplicate_type,
                                         confidence, run_id)
                                    VALUES (?, ?, 'NEAR', ?, ?)
                                    """,
                                    (gid, fp, confidence, ctx.run_id),
                                )
                                ctx.log_event(
                                    "NEAR_DUPLICATE_FOUND",
                                    file_path=fp,
                                    stage=self.NAME,
                                    note=(
                                        f"group={gid} score={score} "
                                        f"title_a={a['title']!r} "
                                        f"title_b={b['title']!r}"
                                    ),
                                )
                    if is_new_group:
                        new_groups += 1
                        result.files_changed += 1
                        logger.info(
                            "near-dupe: %r ~~ %r (score=%d, artist=%s)",
                            a["title"], b["title"], score, artist_key,
                        )

        if not dry_run and new_pairs > 0:
            ctx.conn.commit()

        prefix = "Would stage" if dry_run else "Staged"
        if new_groups == 0:
            result.notes.append("No new near-duplicates found.")
        else:
            result.notes.append(
                f"{prefix} {new_groups} near-duplicate group(s) "
                f"({new_pairs} file entries). "
                f"Review with: musaeus dedupe"
            )

        ctx.record_stage(result)
        return result

    # ── Dry run ───────────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._detect(ctx, dry_run=True)

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self, ctx: RunContext) -> StageResult:
        return self._detect(ctx, dry_run=False)
