#!/usr/bin/env python3
"""
MUSAEUS — Near-Dupe Auto-Resolver
Analyses all NEAR duplicate groups and applies one of four dispositions:

  KEEP_BOTH   — Titles are actually different tracks (live vs studio,
                remix vs original, numbered movement, sequel, etc.)
  AUTO_KEEP   — Clear winner: ALAC beats AAC, larger beats truncated,
                clean name beats prefixed/numbered name
  REVIEW      — Ambiguous: both ALAC or both same codec/size within 5%
  FALSE_POS   — Group flagged as near-dup but titles are clearly different
                (e.g. "Synchronicity I" vs "Synchronicity II")

Actions taken:
  - AUTO_KEEP groups: marks the loser as status=DUPE in archive,
    removes both entries from the duplicates table (group resolved)
  - KEEP_BOTH / FALSE_POS groups: removes from duplicates table
    (no longer pending review)
  - REVIEW groups: leaves in duplicates table for manual review

Usage:
  python scripts/resolve_near_dupes.py --apply  # apply decisions to DB
  python scripts/resolve_near_dupes.py --apply --verbose

Invoking without ``--apply`` is blocked because the legacy dry-run path is
not yet safe.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from musaeus.preview_guard import LEGACY_PREVIEW_HELP, reject_legacy_preview  # noqa: E402

_DEFAULT_DB_PATH = "/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/musaeus.db"


def _fallback_db_path() -> Path:
    """Return the historical environment/default DB path only when live apply is requested."""
    return Path(os.environ.get("MUSAEUS_DB_PATH", _DEFAULT_DB_PATH))


def _resolve_db_path() -> Path:
    """Resolve the configured DB path only for an explicitly requested live apply."""
    try:
        from musaeus.config import get_config

        return get_config().db_path
    except (ImportError, ValueError):
        return _fallback_db_path()


# ── Heuristics ────────────────────────────────────────────────────────────────

# Patterns in titles that mean "keep both" — these are genuinely different tracks
KEEP_BOTH_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(live|concert|unplugged|acoustic|demo|rehearsal)\b", re.I),
    re.compile(r"\b(remix|rmx|re-?mix|extended|radio edit|edit|version|mix)\b", re.I),
    re.compile(r"\b(remaster(?:ed)?|rerecord(?:ed)?|re-?record)\b", re.I),
    re.compile(r"\bpart\s*[12ivxIVX]+\b", re.I),
    re.compile(r"\b(bonus|b-?side|reprise|interlude|intro|outro)\b", re.I),
    re.compile(r"\b(instrumental|a\s*cappella|acapella)\b", re.I),
    re.compile(r"\b(mono|stereo|monaural)\b", re.I),
]

# Roman numerals / numbers in title that distinguish movements or sequels
NUMERAL_PATTERN = re.compile(
    r"(\b[IVX]{1,4}\b|"  # Roman I II III IV V VI VII VIII IX X
    r"\b\d+\b)",  # Arabic digit
    re.I,
)

# Filename prefix patterns: "269. Artist - Title" or "917. Artist - Title"
NUMBERED_PREFIX = re.compile(r"^\d+[\.\-\s]+")


def _norm(s: str) -> str:
    """Lowercase, strip accents, remove punctuation, collapse spaces."""
    s = unicodedata.normalize("NFD", s.lower())
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _extract_numerals(title: str) -> set[str]:
    """Extract all numerals/roman numerals from a title."""
    return {m.group(0).upper() for m in NUMERAL_PATTERN.finditer(title)}


def _has_keep_both_marker(title: str) -> bool:
    return any(p.search(title) for p in KEEP_BOTH_PATTERNS)


def _clean_filename(fp: str) -> str:
    """Strip leading number prefix from bare filename (not full path)."""
    name = Path(fp).stem
    return NUMBERED_PREFIX.sub("", name).strip()


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class Track:
    file_path: str
    artist: str
    title: str
    codec: str  # alac / aac / mp3 / flac / aiff
    bitrate: int
    size_bytes: int

    @property
    def is_lossless(self) -> bool:
        return self.codec in ("alac", "flac", "aiff", "wav", "pcm")

    @property
    def disk_size(self) -> int:
        """Actual disk size — fall back to DB value when DB is 0 (sentinel not run yet)."""
        if self.size_bytes and self.size_bytes > 0:
            return self.size_bytes
        try:
            return os.path.getsize(self.file_path)
        except OSError:
            return 0

    @property
    def quality_score(self) -> int:
        """Higher = better. Lossless wins over lossy; then disk size."""
        base = 1_000_000_000 if self.is_lossless else 0
        return base + self.disk_size


@dataclass
class GroupDecision:
    group_id: str
    disposition: str  # AUTO_KEEP | KEEP_BOTH | FALSE_POS | REVIEW
    keep: str | None  # file_path to keep (AUTO_KEEP only)
    drop: str | None  # file_path to mark DUPE (AUTO_KEEP only)
    reason: str = ""
    tracks: list[Track] = field(default_factory=list)


# ── Decision logic ────────────────────────────────────────────────────────────


def decide(group_id: str, tracks: list[Track]) -> GroupDecision:
    if len(tracks) != 2:
        # More than 2 in a group — unusual; send to review
        return GroupDecision(
            group_id,
            "REVIEW",
            None,
            None,
            f"{len(tracks)} tracks in group — manual review needed",
            tracks,
        )

    a, b = tracks[0], tracks[1]
    title_a = _norm(a.title)
    title_b = _norm(b.title)

    # ── 1. Genuinely different titles (false positive) ────────────────────────
    # Different roman numerals / part numbers means different compositions
    nums_a = _extract_numerals(a.title)
    nums_b = _extract_numerals(b.title)
    if nums_a and nums_b and nums_a != nums_b:
        return GroupDecision(
            group_id,
            "FALSE_POS",
            None,
            None,
            f"Different numerals: {a.title!r} vs {b.title!r}",
            tracks,
        )

    # ── 2. One has a Live/Remix/Remaster/Part marker, other doesn't ──────────
    mark_a = _has_keep_both_marker(a.title)
    mark_b = _has_keep_both_marker(b.title)
    if mark_a != mark_b:
        return GroupDecision(
            group_id,
            "KEEP_BOTH",
            None,
            None,
            f"Different versions: {a.title!r} vs {b.title!r}",
            tracks,
        )

    # ── 3. Both have keep-both markers but different ones ─────────────────────
    if mark_a and mark_b and title_a != title_b:
        return GroupDecision(
            group_id,
            "KEEP_BOTH",
            None,
            None,
            f"Both have version markers: {a.title!r} vs {b.title!r}",
            tracks,
        )

    # ── 4. Clear codec winner (lossless beats lossy) ──────────────────────────
    if a.is_lossless != b.is_lossless:
        winner, loser = (a, b) if a.is_lossless else (b, a)
        return GroupDecision(
            group_id,
            "AUTO_KEEP",
            winner.file_path,
            loser.file_path,
            f"Lossless ({winner.codec}/{winner.size_bytes}B) beats "
            f"lossy ({loser.codec}/{loser.size_bytes}B)",
            tracks,
        )

    # ── 5. Both lossless or both lossy — pick by disk size (larger = more complete)
    sz_a = a.disk_size
    sz_b = b.disk_size
    size_ratio = max(sz_a, sz_b) / max(min(sz_a, sz_b), 1)
    if size_ratio > 1.10:  # >10% size difference → clear winner
        winner, loser = (a, b) if sz_a > sz_b else (b, a)
        reason = (
            f"Larger file ({winner.disk_size:,}B vs {loser.disk_size:,}B, ratio={size_ratio:.2f})"
        )

        # Bonus: prefer the one without a numbered prefix in filename
        a_prefixed = bool(NUMBERED_PREFIX.match(Path(a.file_path).stem))
        b_prefixed = bool(NUMBERED_PREFIX.match(Path(b.file_path).stem))
        if a_prefixed != b_prefixed:
            winner, loser = (b, a) if a_prefixed else (a, b)
            reason += "; clean filename preferred"

        return GroupDecision(
            group_id, "AUTO_KEEP", winner.file_path, loser.file_path, reason, tracks
        )

    # ── 6. Prefer clean filename over numbered/prefixed filename ──────────────
    a_prefixed = bool(NUMBERED_PREFIX.match(Path(a.file_path).stem))
    b_prefixed = bool(NUMBERED_PREFIX.match(Path(b.file_path).stem))
    if a_prefixed != b_prefixed:
        winner, loser = (b, a) if a_prefixed else (a, b)
        return GroupDecision(
            group_id,
            "AUTO_KEEP",
            winner.file_path,
            loser.file_path,
            "Clean filename preferred over numbered prefix",
            tracks,
        )

    # ── 7. Same normalised title, same codec — pick by filename (prefer no artist prefix)
    #    e.g. "Ruby Tuesday.m4a" vs "Rolling Stones - Ruby Tuesday.m4a"
    if title_a == title_b:
        # Prefer file whose stem doesn't contain " - " (artist prefix stripped)
        a_has_prefix = " - " in Path(a.file_path).stem
        b_has_prefix = " - " in Path(b.file_path).stem
        if a_has_prefix != b_has_prefix:
            # Keep the one without the "Artist - Title" prefix (cleaner standalone name)
            winner, loser = (a, b) if not a_has_prefix else (b, a)
            return GroupDecision(
                group_id,
                "AUTO_KEEP",
                winner.file_path,
                loser.file_path,
                f"Same title, prefer clean filename over 'Artist - Title' filename",
                tracks,
            )
        # Both have prefix or both don't — pick larger
        if sz_a != sz_b:
            winner, loser = (a, b) if sz_a > sz_b else (b, a)
            return GroupDecision(
                group_id,
                "AUTO_KEEP",
                winner.file_path,
                loser.file_path,
                f"Same title same codec, larger file wins ({winner.disk_size:,}B vs {loser.disk_size:,}B)",
                tracks,
            )

    # ── 8. One title is a prefix of / truncation of the other ────────────────
    #    e.g. "Under the Br" vs "Under the Bridge" — pick the longer one
    shorter, longer = (a, b) if len(a.title) < len(b.title) else (b, a)
    norm_short = _norm(shorter.title)
    norm_long = _norm(longer.title)
    if norm_long.startswith(norm_short) and len(norm_long) > len(norm_short):
        return GroupDecision(
            group_id,
            "AUTO_KEEP",
            longer.file_path,
            shorter.file_path,
            f"Truncated title: {shorter.title!r} → keeping longer {longer.title!r}",
            tracks,
        )

    # ── 9. Too close to call ──────────────────────────────────────────────────
    return GroupDecision(
        group_id,
        "REVIEW",
        None,
        None,
        f"Similar quality — manual review: {a.title!r} vs {b.title!r}",
        tracks,
    )


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MUSAEUS Near-Dupe Auto-Resolver",
        epilog=f"Without --apply: {LEGACY_PREVIEW_HELP}",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply decisions to DB; legacy dry-run is unavailable",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not args.apply:
        sys.exit(reject_legacy_preview())

    conn = sqlite3.connect(str(_resolve_db_path()))
    conn.row_factory = sqlite3.Row

    # Load all NEAR dupe groups
    rows = conn.execute("""
        SELECT d.group_id, a.file_path, a.artist, a.title,
               a.codec, a.bitrate, a.size_bytes
        FROM duplicates d
        JOIN archive a ON d.file_path = a.file_path
        WHERE d.duplicate_type = 'NEAR'
        ORDER BY d.group_id
    """).fetchall()

    # Group by group_id
    groups: dict[str, list[Track]] = {}
    for row in rows:
        t = Track(
            file_path=row["file_path"],
            artist=row["artist"] or "",
            title=row["title"] or "",
            codec=(row["codec"] or "unknown").lower(),
            bitrate=row["bitrate"] or 0,
            size_bytes=row["size_bytes"] or 0,
        )
        groups.setdefault(row["group_id"], []).append(t)

    # Make decisions
    decisions = [decide(gid, tracks) for gid, tracks in groups.items()]

    # Summarise
    counts: dict[str, int] = {}
    for d in decisions:
        counts[d.disposition] = counts.get(d.disposition, 0) + 1

    print(f"\nNear-Dupe Resolution Summary")
    print(f"  Total groups     : {len(decisions)}")
    print(f"  AUTO_KEEP (clear winner) : {counts.get('AUTO_KEEP', 0)}")
    print(f"  KEEP_BOTH (live/remix)   : {counts.get('KEEP_BOTH', 0)}")
    print(f"  FALSE_POS (diff tracks)  : {counts.get('FALSE_POS', 0)}")
    print(f"  REVIEW (manual needed)   : {counts.get('REVIEW', 0)}")
    print()

    if args.verbose:
        for disp in ("FALSE_POS", "KEEP_BOTH", "REVIEW", "AUTO_KEEP"):
            subset = [d for d in decisions if d.disposition == disp]
            if not subset:
                continue
            label = {
                "AUTO_KEEP": "AUTO-KEEP (will mark loser as DUPE)",
                "KEEP_BOTH": "KEEP BOTH (different versions)",
                "FALSE_POS": "FALSE POSITIVE (different tracks)",
                "REVIEW": "NEEDS REVIEW",
            }[disp]
            print(f"── {label} ({len(subset)}) ─────────────────────────────")
            for d in subset:
                if disp == "AUTO_KEEP":
                    keep_t = next(t for t in d.tracks if t.file_path == d.keep)
                    drop_t = next(t for t in d.tracks if t.file_path == d.drop)
                    print(f"  KEEP  [{keep_t.codec:4}] {keep_t.size_bytes:>10,}B  {keep_t.title}")
                    print(f"  DROP  [{drop_t.codec:4}] {drop_t.size_bytes:>10,}B  {drop_t.title}")
                else:
                    for t in d.tracks:
                        print(f"  [{t.codec:4}] {t.size_bytes:>10,}B  {t.title!r}")
                print(f"  → {d.reason}")
                print()

    # Apply decisions
    auto_kept = 0
    cleared = 0

    for d in decisions:
        if d.disposition == "AUTO_KEEP":
            # Mark loser as DUPE in archive
            conn.execute(
                "UPDATE archive SET status='DUPE' WHERE file_path=?",
                (d.drop,),
            )
            # Remove both from duplicates table
            conn.execute(
                "DELETE FROM duplicates WHERE group_id=?",
                (d.group_id,),
            )
            auto_kept += 1

        elif d.disposition in ("KEEP_BOTH", "FALSE_POS"):
            # Clear from duplicates — no file action needed
            conn.execute(
                "DELETE FROM duplicates WHERE group_id=?",
                (d.group_id,),
            )
            cleared += 1

        # REVIEW: leave in duplicates table untouched

    conn.commit()
    conn.close()

    print(f"\nApplied:")
    print(f"  {auto_kept} group(s) auto-resolved (loser marked DUPE)")
    print(f"  {cleared} group(s) cleared (keep-both / false-positive)")
    print(f"  {counts.get('REVIEW', 0)} group(s) left for manual review")


if __name__ == "__main__":
    main()
