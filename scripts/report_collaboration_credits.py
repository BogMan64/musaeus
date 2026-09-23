#!/usr/bin/env python3
"""
Report artist credits that contain a comma. Never changes anything.

A comma usually separates collaborators ("24kGoldn, iann dior"), but in
some band names it is part of the name. `MetaData/Protected_Band_Names.txt`
is the authority on which, and is consulted first.

That file exists because the first version of this report emitted a
"suggested primary artist" column that turned "Crosby, Stills & Nash"
into "Crosby". It was caught on reading the output, not by a test — the
same false-positive shape that produced 141 bogus truncation candidates
earlier the same day. Splitting a name is easy; knowing whether you may
is the whole problem.

Read-only by construction: opens the database mode=ro.
"""

from __future__ import annotations

import csv
import re
import sqlite3
import sys
from pathlib import Path

VAULT = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT")
DB = VAULT / "musaeus.db"
PROTECTED = VAULT / "MetaData" / "Protected_Band_Names.txt"
OUT = Path.home() / "Desktop" / "MUSAEUS_Collaboration_Credits.csv"

# A band written "A, B & C" tends to end on an ampersand-joined final
# member. Only a hint, and never enough on its own -- the protected file
# is what actually decides.
_BAND_TAIL = re.compile(r",\s*[^,]+\s+(?:&|and)\s+[^,]+$", re.I)
_ARTICLE_TAIL = re.compile(r",\s*(The|A|An|La|Le|El|Los|Las)$", re.I)


def load_protected() -> set[str]:
    if not PROTECTED.exists():
        return set()
    return {
        ln.strip().lower()
        for ln in PROTECTED.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    }


def main() -> int:
    protected = load_protected()
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT artist, genre, COUNT(*) AS tracks, MIN(title) AS example "
        "FROM archive WHERE status='CATALOGUED' AND artist LIKE '%, %' "
        "GROUP BY artist, genre ORDER BY tracks DESC, artist"
    ).fetchall()
    rows = [r for r in rows if not _ARTICLE_TAIL.search(r["artist"] or "")]

    def assess(a: str) -> str:
        if a.strip().lower() in protected:
            return "PROTECTED — never split"
        if _BAND_TAIL.search(a):
            return "probably ONE band — verify before splitting"
        return "probably a collaboration credit"

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["artist_as_stored", "assessment", "genre", "tracks", "example_title"])
        for r in rows:
            w.writerow([r["artist"], assess(r["artist"]), r["genre"], r["tracks"], r["example"]])

    from collections import Counter
    tally = Counter(assess(r["artist"]) for r in rows)
    print(f"{OUT}\n")
    for k, n in tally.most_common():
        tracks = sum(r["tracks"] for r in rows if assess(r["artist"]) == k)
        print(f"  {k:<44} {n:>4} artists, {tracks:>4} tracks")
    print(f"\n  protected names loaded from {PROTECTED.name}: {len(protected)}")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
