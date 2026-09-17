#!/usr/bin/env python3
"""
Split the `Pop` genre into `Classic Pop` / `Modern Pop`.

Owner decision, 2026-08-23: boundary year 2000, and artists holding tracks
on both sides go wholly to whichever era holds MORE of their tracks. That
second rule is what keeps one-genre-per-artist intact -- an era split done
per track would give the same artist two genre values.

Direction of travel is library -> law, never law -> library. The library
rows are rewritten first from the owner's boundary, then MasterLaw is
brought into line with what the library now says. Reversing that would
overwrite owner decisions, as measured on 2026-08-23 (76 of them).

Era comes from `original_year` ONLY -- never from `year`, which is the
edition year and would file Abba (2008-2022 in this library) as Modern Pop.

The gate is per artist, not a coverage percentage over tracks. A blanket
threshold was the wrong shape: the decision is made per artist by majority,
so an artist with four corrected years out of five is decided perfectly
well, while an artist with none cannot be decided at all no matter how
healthy the library-wide number looks. Artists with no recovered year are
left as `Pop` and named in the output. Roughly 15% of tracks never match --
the guards in original_year.py refusing rather than guessing -- so a
library-wide threshold would have blocked forever.

Law rows for artists with no tracks in the library keep `Pop`: there is no
year to place them by, and inventing one to empty the parent genre would
be a guess. `Pop` therefore stays a legal value.

MasterLaw.csv is CRLF and is edited here on bytes -- `sed 's/...$/'` matches
nothing against it, silently.

Usage:
    python3 scripts/apply_era_genres.py            # dry run, writes nothing
    python3 scripts/apply_era_genres.py --apply
"""

from __future__ import annotations

import argparse
import csv
import io
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT")
DB = VAULT / "musaeus.db"
LAW = VAULT / "MetaData" / "MasterLaw.csv"

SOURCE_GENRE = "Pop"
BOUNDARY = 2000
CLASSIC = "Classic Pop"
MODERN = "Modern Pop"


def _year(raw: str | None) -> int | None:
    """A usable 4-digit year, or None. Never a guess."""
    if not raw:
        return None
    y = raw.strip()[:4]
    return int(y) if len(y) == 4 and y.isdigit() else None


def decide(rows: list[sqlite3.Row]) -> tuple[dict[str, str], list[str], list[str]]:
    """Return (artist -> era genre), artists with no usable year, and ties."""
    by_artist: dict[str, list[int]] = defaultdict(list)
    seen: set[str] = set()
    for r in rows:
        artist = (r["artist"] or "").strip()
        if not artist:
            continue
        seen.add(artist)
        y = _year(r["year"])
        if y is not None:
            by_artist[artist].append(y)

    decisions: dict[str, str] = {}
    ties: list[str] = []
    for artist, years in by_artist.items():
        classic = sum(1 for y in years if y < BOUNDARY)
        modern = len(years) - classic
        if classic == modern:
            # A tie has no majority, so the owner's rule does not reach it.
            # Broken toward the artist's EARLIEST recording, which is the
            # era they belong to. Measured 2026-08-24: all four ties here
            # are pre-2000 acts whose second data point is a bad match on a
            # reissue -- Gene Pitney 1962/2019 and Toni Basil 1982/2020 are
            # each the same song twice. Defaulting to Modern filed both
            # wrongly; earliest-year files both correctly.
            ties.append(f"{artist} ({classic}/{modern}, earliest {min(years)})")
            decisions[artist] = CLASSIC if min(years) < BOUNDARY else MODERN
            continue
        decisions[artist] = CLASSIC if classic > modern else MODERN

    undated = sorted(seen - set(by_artist))
    return decisions, undated, sorted(ties)


def sync_law(decisions: dict[str, str], apply: bool) -> tuple[int, int]:
    """Rewrite MasterLaw rows for decided artists. Returns (changed, left)."""
    raw = LAW.read_bytes().decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(raw)))
    header, body = rows[0], rows[1:]

    lookup = {a.strip().lower(): g for a, g in decisions.items()}
    changed = left = 0
    out: list[list[str]] = []
    for row in body:
        if len(row) >= 2 and row[1].strip() == SOURCE_GENRE:
            era = lookup.get(row[0].strip().lower())
            if era:
                row = [row[0], era]
                changed += 1
            else:
                left += 1
        out.append(row)

    if apply:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(LAW, LAW.with_name(f"{LAW.name}.bak-{stamp}"))
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\r\n").writerows([header, *out])
        LAW.write_bytes(buf.getvalue().encode("utf-8"))
    return changed, left


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(archive)").fetchall()}
    if "original_year" not in cols:
        print(
            "REFUSING: archive has no original_year column. `year` here is the\n"
            "edition year, not the recording year -- run `musaeus original-year`\n"
            "first, or this files Abba as Modern Pop."
        )
        return 1

    rows = conn.execute(
        "SELECT artist, original_year AS year FROM archive "
        "WHERE status='CATALOGUED' AND genre=?",
        (SOURCE_GENRE,),
    ).fetchall()
    corrected = sum(1 for r in rows if r["year"])
    print(f"{SOURCE_GENRE}: {len(rows)} tracks, {corrected} with a recovered original year")

    unchecked = conn.execute(
        "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' AND genre=? "
        "AND original_year_checked_at IS NULL",
        (SOURCE_GENRE,),
    ).fetchone()[0]
    if unchecked:
        print(
            f"REFUSING: {unchecked} track(s) have not been checked yet.\n"
            f"Run `musaeus original-year --genre {SOURCE_GENRE}` to completion first."
        )
        return 1

    decisions, undated, ties = decide(rows)
    tally = Counter(decisions.values())
    print(f"artists decided: {len(decisions)}  ->  {dict(tally)}")
    if undated:
        print(f"artists with NO recovered year — left as {SOURCE_GENRE}: {len(undated)}")
        for a in undated:
            print(f"    {a}")
    if ties:
        print(f"exact ties, broken by earliest recording -- review these: {len(ties)}")
        for t in ties:
            print(f"    {t}")

    moved = 0
    for artist, era in sorted(decisions.items()):
        cur = conn.execute(
            "UPDATE archive SET genre=? WHERE status='CATALOGUED' AND genre=? AND artist=?"
            if args.apply
            else "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' AND genre=? AND artist=?",
            (era, SOURCE_GENRE, artist) if args.apply else (SOURCE_GENRE, artist),
        )
        moved += cur.rowcount if args.apply else cur.fetchone()[0]

    law_changed, law_left = sync_law(decisions, args.apply)

    verb = "moved" if args.apply else "would move"
    print(f"\ntracks {verb}: {moved}")
    print(f"law rows {verb}: {law_changed}")
    print(f"law rows left as {SOURCE_GENRE} (no tracks in library): {law_left}")

    if args.apply:
        conn.commit()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' AND genre=?",
            (SOURCE_GENRE,),
        ).fetchone()[0]
        print(f"verify: {SOURCE_GENRE} tracks remaining: {remaining}")
    else:
        print("\nDRY RUN -- nothing written. Re-run with --apply.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
