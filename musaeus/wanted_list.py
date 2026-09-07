#!/usr/bin/env python3
"""
MUSAEUS — turning a knock-off into a wanted-list line for TuneMyMusic.

Why this exists
----------------
The three title-credit patterns that flag a track as junk in
tribute_quarantine.py -- "in the style of X", "originally performed by
X", "made famous by X" -- usually NAME the real artist in the process of
describing the fake one. "Midnight Cruiser [In the Style of Steely Dan]"
is simultaneously evidence the track is a knock-off AND a pointer to the
genuine recording it is standing in for.

Built by hand once, 2026-09-02, as a one-off export (MUSAEUS_WANTED_LIST.txt
on the Desktop) after Grey asked for it verbally. He asked the next day
for it to happen automatically -- this is that, wired into
TributeQuarantineStage so every quarantine run produces its own
TuneMyMusic-ready file alongside the existing manifest, rather than
someone remembering to run a script by hand.

The "already owned" check is what makes this worth building at all.
Measured against the library 2026-09-02: 18 credited knock-offs, 6
already owned properly -- pasting the naive list into TuneMyMusic would
have re-acquired 6 records already on file. Skipping that check would
turn a useful export into one that quietly wastes a third of its own
entries.

What this deliberately does NOT do
-----------------------------------
It does not decide whether a track IS a knock-off -- that judgement stays
in tribute_quarantine.is_junk. This only extracts a NAME from a title
that has already been flagged, and reports whether that name is
already owned. Nine of the eleven credit-extraction false starts in this
project have come from being too eager (an earlier draft's title
stripper caught "Cover Version" and "Backing Track" as artist names) --
so the credit patterns here are the exact three phrases already proven
against the live library, not a broader guess.
"""

from __future__ import annotations

import re
import sqlite3

from .brackets import strip_bracketed

#: The three phrases that both flag a knock-off AND name the original.
#: Verified against the live library 2026-09-02: these three, and only
#: these three, produced zero false positives across 21 extractions --
#: see feedback_no_knockoff_artists.md. A broader pattern (e.g. any
#: trailing parenthetical) is exactly what produced "Cover Version" and
#: "Backing Track" as fake artist names in an earlier draft.
_CREDIT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"in the style of ([^)\]}]+)", re.IGNORECASE),
    re.compile(r"originally performed by ([^)\]}]+)", re.IGNORECASE),
    re.compile(r"(?:as )?made famous by[ \-]+([^)\]}]+)", re.IGNORECASE),
]

#: Trailing markup that survives bracket-stripping because it uses a
#: dash rather than a bracket -- "Song - Sound-A-Like As Made Famous By
#: X" -- plus the karaoke/instrumental labels the credit patterns above
#: leave behind once the credited name is removed.
_TRAILING_MARKUP_RE = re.compile(
    r"\s*-\s*(sound-?a-?like|karaoke|instrumental).*", re.IGNORECASE
)


def extract_credited_artist(title: str) -> str | None:
    """The real artist named inside a knock-off's own title, or None.

    >>> extract_credited_artist("Midnight Cruiser [In the Style of Steely Dan]")
    'Steely Dan'
    >>> extract_credited_artist("Just a Regular Song")
    """
    for pattern in _CREDIT_PATTERNS:
        m = pattern.search(title or "")
        if m:
            name = m.group(1).strip(" .]")
            name = re.sub(r"\s*[-–]\s*$", "", name)
            return name or None
    return None


def clean_title(title: str) -> str:
    """The song title with the knock-off's own markup removed.

    >>> clean_title("Midnight Cruiser [In the Style of Steely Dan]")
    'Midnight Cruiser'
    >>> clean_title("Nuthin' But a G Thang - Sound-a-Like As Made Famous By - Dr. Dre")
    "Nuthin' But a G Thang"
    """
    t = strip_bracketed(title or "")
    t = _TRAILING_MARKUP_RE.sub("", t)
    return t.strip(" -")


def already_owned(
    conn: sqlite3.Connection,
    artist: str,
    title: str,
    exclude_path: str | None = None,
) -> bool:
    """True when a CATALOGUED row OTHER THAN exclude_path carries this
    artist and title.

    A prefix LIKE match, not exact -- real-world variance in remaster
    tags and punctuation means an exact match under-counts what is
    genuinely already owned. The prefix length (14 chars) is the same
    figure measured against the live library 2026-09-02 that correctly
    matched all 6 already-owned cases with no false matches in that run.

    `exclude_path` exists because the caller is usually asking about a file
    that is ITSELF in the archive (P1-1). A CATALOGUED row whose audio_hash
    went NULL is re-hashed by sentinel; if it now fails to decode, the tags
    read off that same file match its own row, "already owned" returns True,
    and nothing is written. The worst case there is a library master that
    has gone bad -- exactly the case the wanted list exists for, and the one
    it silently dropped. Excluded by file_path rather than rowid because
    that is what the caller holds at that point.
    """
    if not artist or not title:
        return False
    sql = """
        SELECT 1 FROM archive
         WHERE status = 'CATALOGUED'
           AND lower(artist) LIKE ?
           AND lower(title) LIKE ?
    """
    params: list[str] = [f"%{artist.lower()[:14]}%", f"%{title.lower()[:14]}%"]
    if exclude_path:
        sql += "   AND file_path <> ?\n"
        params.append(str(exclude_path))
    row = conn.execute(sql + " LIMIT 1", params).fetchone()
    return row is not None


def wanted_lines(conn: sqlite3.Connection, quarantined: list[dict]) -> list[str]:
    """"Artist - Title" lines for TuneMyMusic, from a batch of quarantined
    rows.

    *quarantined* items need "title" (and may have "artist", used only
    for logging/debugging, not for the credit extraction itself -- the
    credited artist comes from the TITLE, because that is where the
    knock-off names the original). Only rows where a credit was found
    AND the real artist/title combination is not already owned appear
    in the result. Order follows *quarantined*'s own order.
    """
    lines: list[str] = []
    for item in quarantined:
        title = item.get("title") or ""
        credited = extract_credited_artist(title)
        if not credited:
            continue
        song = clean_title(title)
        if not song:
            continue
        if already_owned(conn, credited, song):
            continue
        lines.append(f"{credited} - {song}")
    return lines
