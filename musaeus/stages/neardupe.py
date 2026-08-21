#!/usr/bin/env python3
"""
MUSAEUS — Stage: NearDupe
Metadata-based near-duplicate detection (no audio fingerprinting required).

What it does:
  - Loads all CATALOGUED archive rows
  - Groups tracks by normalised artist name (ArtistCanon → fuzzy ≥88)
  - Within each artist group, strips version/edition qualifiers from
    titles (Remaster, Live, Remix, Acoustic, etc. — ORPHEUS-compatible,
    see STRIP_WORDS below) before comparing normalised title pairs with
    rapidfuzz fuzz.ratio — flags pairs scoring ≥ TITLE_THRESHOLD (88)
  - Guards against merging two different live recordings of the same
    song into one group (both sides carry a live marker in the raw,
    pre-strip title → skipped, since "live" is stripped before scoring
    and would otherwise make them look identical)
  - Stages flagged pairs in the duplicates table with type='NEAR'
  - Skips pairs already in the EXACT duplicates table
  - dry_run() reports matches without writing to DB
  - Re-run safe: INSERT OR IGNORE on (group_id, file_path)

Design decisions (ported from ORPHEUS's SCRIPTS/lib/orpheus_fuzzy.py):
  - Artist normalisation: lowercase, strip punctuation, collapse whitespace
  - Title normalisation: strip version/edition brackets and bare
    STRIP_WORDS, then same as artist + strip leading "the "
  - group_id: "near_{sha8}" where sha8 is SHA-256[:8] of sorted file paths
  - Confidence: the fuzz.ratio score (0.0–1.0)
  - O(n²) within artist groups — fast because groups are small (< 200 tracks)
  - Threshold is conservative at 88 to avoid false positives; the
    stripping (not the threshold) is what catches "Yesterday" vs
    "Yesterday (Remaster)" and similar version-variant pairs — a bare
    fuzz.ratio on unstripped titles scores well below 88 for these
    (e.g. "Yesterday" vs "Yesterday (Remaster)" = 66.7)

The resulting near-duplicate groups appear in `musaeus dedupe` alongside
exact duplicates.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata

try:
    from rapidfuzz import fuzz

    _HAVE_RAPIDFUZZ = True
except ImportError:
    _HAVE_RAPIDFUZZ = False

from ..canon import ArtistCanon
from ..context import RunContext, StageResult
from .base import BaseStage, StageError

logger = logging.getLogger(__name__)

# Minimum fuzz.ratio to flag as near-duplicate (0–100 scale)
TITLE_THRESHOLD = 88
ARTIST_THRESHOLD = 88

# Version/edition words stripped from titles before fuzzy comparison.
# ORPHEUS-compatible (SCRIPTS/lib/orpheus_fuzzy.py STRIP_WORDS).
STRIP_WORDS: tuple[str, ...] = (
    "remastered",
    "remaster",
    "remix",
    "live",
    "acoustic",
    "radio edit",
    "single version",
    "album version",
    "mono",
    "stereo",
    "demo",
    "karaoke",
)

# Brackets containing ONLY version words (+ optional year/digits/punctuation)
# are safe to strip. Deliberately NOT stripping all bracketed content —
# arbitrary parentheticals can be part of the real title (e.g. "Here I Am
# (Come and Take Me)" must not collapse to "Here I Am").
_VERSION_BRACKET_WORDS = re.compile(
    r"[\(\[]\s*(?:(?:19|20)\d{2}\s+)?(?:"
    + r"|".join(re.escape(w) for w in sorted(STRIP_WORDS, key=len, reverse=True))
    + r")[\s\d,./+-]*[\)\]]",
    re.IGNORECASE,
)
_YEAR_BRACKET_RE = re.compile(r"[\(\[]\s*(?:19|20)\d{2}\s*[\)\]]")  # "(2015)"
_NUM_BRACKET_RE = re.compile(r"[\(\[]\s*\d{1,2}\s*[\)\]]")  # "[2]"

# Same words also stripped as bare whole-words (catches "Song Title Remix"
# with no surrounding parens at all).
_STRIP_WORDS_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in sorted(STRIP_WORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

# Raw (pre-strip) live-recording marker, used to avoid merging two
# different live recordings of the same song into one near-dupe group.
_LIVE_MARKER_RE = re.compile(r"\b(live|in concert|at the)\b", re.IGNORECASE)


def _has_live_marker(raw_title: str) -> bool:
    """True if the raw (unstripped) title looks like a live recording."""
    return bool(_LIVE_MARKER_RE.search(raw_title))


def _strip_title_qualifiers(s: str) -> str:
    """Remove version/edition brackets and bare STRIP_WORDS from a title."""
    s = _VERSION_BRACKET_WORDS.sub(" ", s)
    s = _YEAR_BRACKET_RE.sub(" ", s)
    s = _NUM_BRACKET_RE.sub(" ", s)
    s = _STRIP_WORDS_RE.sub(" ", s)
    return s


def _normalise(s: str, strip_qualifiers: bool = False) -> str:
    """
    Lowercase, NFD-normalise, strip punctuation, collapse whitespace.

    strip_qualifiers=True additionally removes version/edition words
    (Remaster, Live, Remix, etc.) — used for titles, not artist names.
    """
    if strip_qualifiers:
        s = _strip_title_qualifiers(s)
    s = unicodedata.normalize("NFD", s.lower())
    s = re.sub(r"[^\w\s]", " ", s)  # punctuation → space
    s = re.sub(r"\s+", " ", s).strip()  # collapse whitespace
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

        cfg = ctx.config
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

                    # Don't merge two different live recordings of the same
                    # song — "live" is stripped before scoring below, so
                    # without this guard they'd look identical and collapse
                    # into one group. Studio-vs-live still matches fine
                    # (only one side carries the marker).
                    if _has_live_marker(a["title"]) and _has_live_marker(b["title"]):
                        continue

                    title_a = _normalise(a["title"], strip_qualifiers=True)
                    title_b = _normalise(b["title"], strip_qualifiers=True)

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
                            a["title"],
                            b["title"],
                            score,
                            artist_key,
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
