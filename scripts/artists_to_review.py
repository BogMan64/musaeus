#!/usr/bin/env python3
"""
MUSAEUS — Artists To Review

Writes ArtistsToReview.csv next to TuneMyMusic.csv, listing every artist in
the library that NO authority has ever heard of: absent from MasterLaw.csv
and absent from artist_canon.tsv, in either direction.

Why this file exists
--------------------
Grey, 2026-09-07: "This way the enduser can look at new artist if any ever
are spotted and can decide the next move."

An artist nobody has ruled on is the shape every knock-off arrives in. The
first run found 41 artists across 127 tracks, and the top of the list was
"Party Tyme" (16 tracks, a karaoke brand), "Stephen Mc-Queen" (a Fats
Domino medley), "Johnny Carroll" (a Nat King Cole cover) and "Various
Artists" -- beside Lizzo, The Monks and USA for Africa, which are real.
That mix is the point: the file cannot decide, and does not try to.

Deliberately NOT named AddToArtistCanon
---------------------------------------
The canon maps a raw name to a canonical one. That is only one of the three
moves available here, and naming the file after it would quietly steer every
decision towards a rename:

    KEEP    -- a real artist. Add to MasterLaw.csv with a genre.
    REMOVE  -- a knock-off. Delete, and deny the hash so it stays gone.
    RENAME  -- the same act under another name. Add to artist_canon.tsv.

The DECISION column names all three, so the file asks the real question.

Read-only. Writes one CSV and nothing else.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.canon.genre_law import GenreLaw  # noqa: E402
from musaeus.config import MusicConfig  # noqa: E402

def fold(name: str) -> str:
    """Article-folding lookup key.

    Reuses GenreLaw's, rather than reimplementing it. Both sides of every
    comparison have to fold identically or "Byrds, The" looks unknown while
    "The Byrds" sits in the law -- that exact mismatch left 246 MasterLaw
    rules dormant once, and a second copy of the rule is how it would come
    back. protected_artists.py exists because four copies of a different
    list had already diverged; this is the same lesson, applied before
    rather than after.
    """
    return GenreLaw._key(name or "")


def known_names(cfg: MusicConfig) -> set[str]:
    """Every artist any authority has an opinion about."""
    known: set[str] = set()
    law = cfg.meta_dir / "MasterLaw.csv"
    if law.exists():
        with law.open(encoding="utf-8") as fh:
            known |= {fold(r[0]) for r in csv.reader(fh) if r and r[0].strip()}
    canon = cfg.meta_dir / "artist_canon.tsv"
    if canon.exists():
        for line in canon.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#") or "\t" not in line:
                continue
            raw, canonical = line.split("\t")[:2]
            # BOTH sides: a name that is only ever a rename TARGET is still known.
            known.add(fold(raw))
            known.add(fold(canonical))
    known.discard("")
    return known


def collect(cfg: MusicConfig) -> list[dict]:
    known = known_names(cfg)
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    counts: Counter = Counter()
    example: dict[str, str] = {}
    for r in conn.execute(
        "SELECT artist, title FROM archive WHERE status='CATALOGUED' "
        "AND artist IS NOT NULL AND trim(artist) != ''"
    ):
        counts[r["artist"]] += 1
        example.setdefault(r["artist"], r["title"] or "")
    conn.close()
    rows = [
        {
            "DECISION (KEEP / REMOVE / RENAME <name>)": "",
            "genre_if_KEEP": "",
            "artist": a,
            "tracks": n,
            "example_track": example.get(a, ""),
        }
        for a, n in counts.items()
        if fold(a) not in known
    ]
    rows.sort(key=lambda r: (-r["tracks"], r["artist"].lower()))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--print", action="store_true", help="also print to stdout")
    args = ap.parse_args()

    cfg = MusicConfig.from_env()
    rows = collect(cfg)
    out = cfg.alac_archive / "ArtistsToReview.csv"
    if not rows:
        print("Every catalogued artist is known to MasterLaw or the canon.")
        if out.exists():
            out.unlink()
            print(f"Removed the now-empty {out.name}.")
        return 0

    fields = list(rows[0].keys())
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
        fh.write("\n")
        fh.write("# DECISION: KEEP (real -- add to MasterLaw.csv, fill genre_if_KEEP)\n")
        fh.write("#           REMOVE (knock-off -- delete and deny the hash)\n")
        fh.write("#           RENAME <name> (same act -- add to artist_canon.tsv)\n")

    print(
        f"{len(rows)} artist(s) unknown to every authority, "
        f"{sum(r['tracks'] for r in rows)} track(s) -> {out}"
    )
    if args.print:
        for r in rows:
            print(f"  {r['tracks']:4}t  {r['artist'][:40]:40}  e.g. {r['example_track'][:34]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
