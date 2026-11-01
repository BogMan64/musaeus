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

from .brackets import CLOSE, OPEN, strip_bracketed

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
_TRAILING_MARKUP_RE = re.compile(r"\s*-\s*(sound-?a-?like|karaoke|instrumental).*", re.IGNORECASE)


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


#: Words of an edition label, never of a song list: "(Live/Edit)", "- Single Version/Edit".
VERSION_LABEL_RE = re.compile(
    r"\b(?:live|edit|version|mix|remaster\w*|mono|stereo|single|re-?record\w*|demo|acoustic)\b",
    re.IGNORECASE,
)
_SONG_LIST_RE = re.compile(r"[/;]")
#: Text just after the word "Medley" is its song list, even a song named "Live And Let Die".
_AFTER_MEDLEY_RE = re.compile(r"\bmedley\W*$", re.IGNORECASE)


def strip_label_suffix(title: str) -> str:
    """The title without an edition label after its last " - ": "Song - Live/Edit"."""
    head, sep, tail = (title or "").rpartition(" - ")
    return (
        head
        if sep and VERSION_LABEL_RE.search(tail) and not _AFTER_MEDLEY_RE.search(head)
        else title or ""
    )


def medley_songs(title: str) -> list[str]:
    """The songs a medley's title names.

    >>> medley_songs("Medley: Little Sister / Get Back (August 12 - Midnight Show)")
    ['Little Sister', 'Get Back']
    >>> medley_songs("Medley (Little Sister / Get Back)")
    ['Little Sister', 'Get Back']
    >>> medley_songs("Little Sister / Get Back Medley")
    ['Little Sister', 'Get Back']
    >>> medley_songs("Cape Breton Medley")
    []
    """
    t = title or ""
    # A bracket holding the song list, not a label (CodeRabbit, review of #271).
    lists = [m.group(1) for m in re.finditer(rf"[{OPEN}]([^{OPEN}{CLOSE}]*)[{CLOSE}]", t)
             if _SONG_LIST_RE.search(m.group(1))
             and (_AFTER_MEDLEY_RE.search(t[: m.start()]) or not VERSION_LABEL_RE.search(m.group(1)))]  # fmt: skip
    t = lists[0] if lists else strip_label_suffix(strip_bracketed(t))
    trailing = re.search(r"\s*\bmedley\s*$", t, re.IGNORECASE)
    if trailing:
        # "Little Sister / Get Back Medley" names its songs; "Cape Breton Medley" does not.
        t = t[: trailing.start()] if _SONG_LIST_RE.search(t) else ""
    t = re.sub(r"^.*?\bmedley\b\s*[:-]?\s*", "", t, flags=re.IGNORECASE)
    return [p.strip(" :-") for p in re.split(r"\s*[/;]\s*", t) if p.strip(" :-")]


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
    """ "Artist - Title" lines for TuneMyMusic, from a batch of quarantined
    rows.

    *quarantined* items need "title" (and may have "artist", not used for
    the credit extraction itself -- the credited artist comes from the
    TITLE, because that is where the knock-off names the original). Only
    rows where a credit was found AND the real artist/title combination is
    not already owned appear in the result. A medley (reason "medley") lists
    each song it names under its own "artist" instead. Order follows
    *quarantined*'s own order.
    """
    lines: list[str] = []
    for item in quarantined:
        title = item.get("title") or ""
        artist = item.get("artist") or ""
        if item.get("reason") == "medley":
            # Each song a medley names, by the medley's own artist (Grey,
            # 2026-10-31: "place individual tracks onto tunemymusic").
            lines += [f"{artist} - {s}" for s in medley_songs(title)
                      if artist and not already_owned(conn, artist, s)]  # fmt: skip
            continue
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
