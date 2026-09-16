#!/usr/bin/env python3
"""Measure what ListenBrainz pass 1 actually returns, before pass 2 is written.

The two-pass design rests on an assumption: that
GET /1/popularity/top-recordings-for-artist/{artist_mbid} returns enough per
artist to narrow the field, so MusicBrainz only has to adjudicate survivors.
That assumption is worth one measurement rather than a paragraph of confidence
-- the whole point of this exercise being that coverage and meaning are
different things.

READ-ONLY EVERYWHERE.
  - the vault database is opened mode=ro, and only mb_cache.db is read
  - ListenBrainz popularity endpoints are public, no auth, no writes
  - output goes to /tmp only

What it reports, per artist:
  recordings returned      -- is there a cap, and where
  distinct song titles     -- how many songs those cover
  titles with >1 version   -- THE number that matters: pass 1 can only narrow
                              a field where it returns more than one pressing
  lengths present          -- can the scorer use its length rules
  listen counts present    -- is the popularity signal actually populated
"""

from __future__ import annotations

import json
import re

from musaeus.brackets import CLOSE, OPEN
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

MB_CACHE = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/_db_backups/mb_cache.db")
LB = "https://api.listenbrainz.org/1/popularity/top-recordings-for-artist/{}"
UA = "fm-radio-lab/0.1 (research probe; grey@localhost)"
OUT = Path("/tmp/pass1_probe.md")

#: Politeness. ListenBrainz is more generous than MusicBrainz's 1/sec, but
#: this is someone else's free infrastructure and a probe has no deadline.
PAUSE = 1.0


def artist_mbids(limit: int) -> list[tuple[str, str]]:
    """(name, mbid) for artists MusicBrainz actually resolved.

    Read from mb_cache.db rather than the live musaeus.db: it is the smaller
    file, it is the one that holds the mbids, and it keeps this probe away
    from the database a pipeline stage might be writing.
    """
    if not MB_CACHE.exists():
        print(f"no mb cache at {MB_CACHE}", file=sys.stderr)
        return []
    conn = sqlite3.connect(f"file:{MB_CACHE}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT mb_name, mbid FROM mb_artist "
            "WHERE found = 1 AND mbid IS NOT NULL AND trim(mbid) != '' "
            "ORDER BY mb_name LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [(r["mb_name"] or "?", r["mbid"]) for r in rows]


def fetch(mbid: str):
    req = urllib.request.Request(LB.format(mbid), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8")), ""
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except Exception as exc:  # network, timeout, malformed JSON
        return None, f"{type(exc).__name__}: {exc}"


#: Strip a version qualifier so two pressings of one song group together.
#: Same idea as MUSAEUS's neardupe title-qualifier stripping; kept local and
#: simple because this is a measurement, not the shipping grouper.
# Bracket classes from musaeus.brackets, not a private copy. Each of
# these covered parens and squares but NOT braces -- the omission that
# rule exists to catch, and invisible while this lived outside the repo.
_QUAL = re.compile(
    rf"\s*[{OPEN}][^{CLOSE}]*"
    r"(remaster|remix|live|mono|stereo|version|edit|mix|take|demo|instrumental)"
    rf"[^{CLOSE}]*[{CLOSE}]",
    re.I,
)


def base_title(t: str) -> str:
    prev = None
    out = t or ""
    while out != prev:
        prev = out
        out = _QUAL.sub("", out).strip()
    return out.casefold()


def main(limit: int = 12) -> None:
    artists = artist_mbids(limit)
    if not artists:
        print("no artist mbids available -- nothing to probe", file=sys.stderr)
        return

    lines = [
        "# ListenBrainz pass-1 coverage probe",
        "",
        f"Artists sampled: {len(artists)} (from mb_cache.db, found=1)",
        "Endpoint: GET /1/popularity/top-recordings-for-artist/{artist_mbid}",
        "Read-only. No writes anywhere. No auth required.",
        "",
        "| artist | recordings | songs | songs with >1 version | lengths | listens | note |",
        "|---|---|---|---|---|---|---|",
    ]
    totals = Counter()
    caps = []

    for name, mbid in artists:
        data, err = fetch(mbid)
        time.sleep(PAUSE)
        if err or data is None:
            lines.append(f"| {name} | — | — | — | — | — | {err or 'no data'} |")
            totals["failed"] += 1
            continue

        n = len(data)
        caps.append(n)
        groups = Counter(base_title(r.get("recording_name", "")) for r in data)
        multi = sum(1 for c in groups.values() if c > 1)
        with_len = sum(1 for r in data if r.get("length"))
        with_plays = sum(1 for r in data if r.get("total_listen_count"))

        totals["recordings"] += n
        totals["songs"] += len(groups)
        totals["multi"] += multi
        totals["with_len"] += with_len
        totals["with_plays"] += with_plays
        totals["ok"] += 1

        lines.append(
            f"| {name} | {n} | {len(groups)} | **{multi}** | "
            f"{with_len}/{n} | {with_plays}/{n} | |"
        )

    lines += [
        "",
        "## Totals",
        "",
        f"- artists queried OK: {totals['ok']}, failed: {totals['failed']}",
        f"- recordings returned: {totals['recordings']}",
        f"- distinct songs: {totals['songs']}",
        f"- **songs with more than one version returned: {totals['multi']}**",
        f"- recordings carrying a length: {totals['with_len']}",
        f"- recordings carrying a listen count: {totals['with_plays']}",
        "",
        "## Reading this",
        "",
        "The number that decides the two-pass design is *songs with more than",
        "one version returned*. Pass 1 can only narrow a field where it returns",
        "more than one pressing of the same song. Where it returns exactly one,",
        "pass 1 has told us what is popular but nothing about alternatives, so",
        "MusicBrainz must still enumerate them -- and the hoped-for saving does",
        "not materialise for that song.",
        "",
        f"Per-artist recording counts: {sorted(caps, reverse=True)[:20]}",
        "A single repeated value at the top of that list is an API cap, not a",
        "property of the catalogue.",
    ]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[-18:]))
    print(f"\nfull report: {OUT}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 12)
