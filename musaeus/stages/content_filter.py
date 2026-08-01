#!/usr/bin/env python3
"""
MUSAEUS — Content Filter
Rejects junk content at intake: meditation, karaoke, workout mixes,
sleep hypnosis, sound baths, tribute bands, and non-music audio.

Used by IngestStage to skip files before they enter the archive.
Also available as a standalone audit tool.

Usage (standalone):
  python -m musaeus.stages.content_filter          # audit current inbox
  python -m musaeus.stages.content_filter --purge  # remove junk from archive DB
"""

from __future__ import annotations

import re
from pathlib import Path


# ── Artist patterns that indicate non-original/non-music content ──────────────
JUNK_ARTIST_PATTERNS: list[re.Pattern] = [
    # Karaoke / tribute / cover compilations
    re.compile(r"karaoke", re.I),
    re.compile(r"tribute", re.I),
    re.compile(r"stingray music", re.I),
    re.compile(r"party tyme", re.I),
    re.compile(r"paris music", re.I),
    re.compile(r"done again", re.I),
    re.compile(r"ameritz", re.I),
    re.compile(r"musical creations", re.I),
    re.compile(r"avid professional", re.I),
    re.compile(r"hit crew", re.I),
    re.compile(r"pop feast", re.I),
    re.compile(r"studio allstars", re.I),
    re.compile(r"studio group", re.I),
    re.compile(r"pop princess", re.I),
    re.compile(r"megarock hits", re.I),
    re.compile(r"dj playback", re.I),
    re.compile(r"silver disco explosion", re.I),
    re.compile(r"70s music all stars", re.I),
    re.compile(r"players since creation", re.I),

    # Meditation / sleep / wellness
    re.compile(r"michael sealey", re.I),
    re.compile(r"jason stephenson", re.I),
    re.compile(r"deep sleep", re.I),
    re.compile(r"healing vibrations", re.I),
    re.compile(r"health therapies", re.I),
    re.compile(r"lynn buddhist", re.I),
    re.compile(r"lofi worship", re.I),
    re.compile(r"brayan brain waves", re.I),
    re.compile(r"holly holmes", re.I),
    re.compile(r"guided meditation", re.I),
    re.compile(r"study music", re.I),
    re.compile(r"mindful movement", re.I),

    # Workout / ambient
    re.compile(r"workout music", re.I),
    re.compile(r"108 music", re.I),

    # Sound-alike / instrumental covers
    re.compile(r"musica instrumental", re.I),
    re.compile(r"orgel sound", re.I),
    re.compile(r"8d tunes", re.I),
]

# ── Title patterns that indicate junk content ─────────────────────────────────
JUNK_TITLE_PATTERNS: list[re.Pattern] = [
    re.compile(r"karaoke", re.I),
    re.compile(r"in the style of", re.I),
    re.compile(r"originally performed", re.I),
    re.compile(r"sound-a-like", re.I),
    re.compile(r"backing track", re.I),
    re.compile(r"demonstration version", re.I),
    re.compile(r"\bvocal version\b.*style", re.I),
    re.compile(r"workout mix", re.I),
    re.compile(r"sleep meditation", re.I),
    re.compile(r"hypnosis", re.I),
    re.compile(r"sound bath", re.I),
    re.compile(r"cranial nerve", re.I),
    re.compile(r"letting go of", re.I),
    re.compile(r"cleanse trauma", re.I),
    re.compile(r"healing music", re.I),
    re.compile(r"healing through", re.I),
    re.compile(r"corporate success", re.I),
    re.compile(r"deep spiritual", re.I),
    re.compile(r"baroque music for concentration", re.I),
]


def is_junk_content(filename: str) -> bool:
    """
    Determine if a file should be rejected based on its filename.
    Parses 'Artist - Title.ext' from the stem and checks both parts.
    Returns True if the content is junk (should be rejected/quarantined).
    """
    stem = Path(filename).stem

    # Check full stem against artist patterns
    for pattern in JUNK_ARTIST_PATTERNS:
        if pattern.search(stem):
            return True

    # If the filename has " - " separator, check artist and title separately
    if " - " in stem:
        artist, title = stem.split(" - ", 1)

        for pattern in JUNK_ARTIST_PATTERNS:
            if pattern.search(artist):
                return True

        for pattern in JUNK_TITLE_PATTERNS:
            if pattern.search(title):
                return True

    else:
        # No separator — check the whole stem against title patterns too
        for pattern in JUNK_TITLE_PATTERNS:
            if pattern.search(stem):
                return True

    return False


def is_junk_by_fields(artist: str, title: str) -> bool:
    """
    Check artist and title strings directly (for DB-level audit).
    Returns True if the content is junk.
    """
    for pattern in JUNK_ARTIST_PATTERNS:
        if pattern.search(artist):
            return True
    for pattern in JUNK_TITLE_PATTERNS:
        if pattern.search(title):
            return True
    return False


# ── Standalone mode ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sqlite3
    import sys
    import os

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from musaeus.config import get_config

    parser = argparse.ArgumentParser(description="MUSAEUS Content Filter — audit/purge junk")
    parser.add_argument("--purge", action="store_true",
                        help="Remove junk entries from archive (moves to QUARANTINE status)")
    args = parser.parse_args()

    cfg = get_config()
    conn = sqlite3.connect(str(cfg.db_path))
    conn.row_factory = sqlite3.Row

    rows = conn.execute("SELECT id, file_path, artist, title FROM archive").fetchall()

    junk_found: list[dict] = []
    for row in rows:
        artist = row["artist"] or ""
        title = row["title"] or ""
        filename = Path(row["file_path"]).name

        if is_junk_content(filename) or is_junk_by_fields(artist, title):
            junk_found.append(dict(row))

    print(f"MUSAEUS Content Filter Audit")
    print(f"  DB: {cfg.db_path}")
    print(f"  Total archive entries: {len(rows)}")
    print(f"  Junk detected: {len(junk_found)}")
    print()

    if junk_found:
        for item in junk_found[:30]:
            print(f"  JUNK: {item['artist']} - {item['title']}")
        if len(junk_found) > 30:
            print(f"  ... and {len(junk_found) - 30} more")

    if args.purge and junk_found:
        for item in junk_found:
            conn.execute(
                "UPDATE archive SET status = 'QUARANTINE' WHERE id = ?",
                (item["id"],),
            )
        conn.commit()
        print(f"\n  Purged {len(junk_found)} junk entries (status → QUARANTINE)")
    elif not args.purge and junk_found:
        print(f"\n  Pass --purge to quarantine these entries.")

    conn.close()
