#!/usr/bin/env python3
"""
Assign comma-joined collaboration credits to a single artist.

Grey's rule, 2026-08-24: pick whichever component artist the library
already holds the most tracks by. If none of them are held, or they tie,
take the first name in the credit.

That ordering matters. It consolidates onto artists already collected
rather than minting a new single-track artist for every guest feature,
and "first" is only the fallback -- a credit like "Jamie xx, Romy" should
land on whichever of the two the owner actually collects, not on
whichever the tagger happened to list first.

Two exclusions, both deliberate:

  Protected band names (MetaData/Protected_Band_Names.txt) contain a
  comma as part of the name -- "Crosby, Stills & Nash" is not a credit
  to be resolved.

  Classical is handled by the composer rule instead
  (attribute_classical_to_composer.py); a Bach concerto should be filed
  under Bach, not under whichever soloist has the most other recordings.

Splitting is on commas only. "&" groups are left whole, so
"uChill, Smokey Robinson & The Miracles, Somni" offers
"Smokey Robinson & The Miracles" as one candidate rather than two.

Usage:
    python3 scripts/resolve_collaboration_credits.py            # dry run
    python3 scripts/resolve_collaboration_credits.py --apply
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.stages.organize import (  # noqa: E402
    build_track_filename,
    sanitize_path_component,
    unique_path,
)

VAULT = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT")
DB = VAULT / "musaeus.db"
PROTECTED = VAULT / "MetaData" / "Protected_Band_Names.txt"
LAW = VAULT / "MetaData" / "MasterLaw.csv"

# Mirrors normalize.py's own article list. "Ceresband, De" and
# "Bravos, Los" are article folds of "De Ceresband" and "Los Bravos" --
# one artist each, not a credit to be split.
_ARTICLE_TAIL = re.compile(
    r",\s*(the|a|an|le|la|les|el|los|las|de|het|een|die|das|ein|eine)$", re.I
)

# "X, The Y" is an "X and The Y" credit that lost its conjunction: one act,
# not a collaboration. Found in the dry run -- "ALVIN, The Chipmunks",
# "Clyde McPhatter, The Drifters", "Bobby Boris Pickett, The Crypt-Kickers".
# Resolving these by track count would file the Chipmunks under "ALVIN".
# 21 of them; reported rather than guessed at, since restoring the right
# name needs a human.
_BACKING_BAND = re.compile(r",\s*The\s+\S", re.I)


def load_protected() -> set[str]:
    if not PROTECTED.exists():
        return set()
    return {
        ln.strip().lower()
        for ln in PROTECTED.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    protected = load_protected()
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # How many tracks the library holds per artist, as it stands.
    counts = {
        r["artist"].strip().lower(): r["n"]
        for r in conn.execute(
            "SELECT artist, COUNT(*) AS n FROM archive WHERE status='CATALOGUED' "
            "AND artist IS NOT NULL GROUP BY artist"
        )
    }

    rows = conn.execute(
        "SELECT id, artist, title, genre, file_path FROM archive "
        "WHERE status='CATALOGUED' AND artist LIKE '%, %' AND genre != 'Classical' "
        "ORDER BY artist, title"
    ).fetchall()

    plan, reasons, skipped_bands = [], Counter(), set()
    for r in rows:
        credit = (r["artist"] or "").strip()
        if credit.lower() in protected or _ARTICLE_TAIL.search(credit):
            continue
        if _BACKING_BAND.search(credit):
            skipped_bands.add(credit)
            continue
        parts = [p.strip() for p in credit.split(",") if p.strip()]
        if len(parts) < 2:
            continue
        scored = [(counts.get(p.lower(), 0), -i, p) for i, p in enumerate(parts)]
        best_n, _, winner = max(scored)
        reasons["held by the library" if best_n else "none held — first in credit"] += 1
        if winner != credit:
            plan.append((r, winner, best_n))

    print(f"collaboration credits to resolve: {len(plan)}")
    print(f"  {dict(reasons)}\n")
    print(f"  {'credit as stored':<44} -> {'assigned to':<26} tracks held")
    print("  " + "-" * 84)
    for r, winner, n in plan[:20]:
        print(f"  {(r['artist'] or '')[:43]:<44} -> {winner[:25]:<26} {n}")
    if len(plan) > 20:
        print(f"  ... and {len(plan) - 20} more")

    if skipped_bands:
        print(f"\n  SKIPPED — \"X, The Y\" looks like one act, not a credit ({len(skipped_bands)}):")
        for c in sorted(skipped_bands)[:10]:
            print(f"    {c}")
        if len(skipped_bands) > 10:
            print(f"    ... and {len(skipped_bands) - 10} more")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    moved = 0
    for r, winner, _ in plan:
        src = Path(r["file_path"])
        if not src.exists():
            continue
        album_dir, artist_dir = src.parent, src.parent.parent
        dst_dir = artist_dir.with_name(sanitize_path_component(winner)) / album_dir.name
        dst = dst_dir / build_track_filename(winner, r["title"] or src.stem, src.suffix)
        # unique_path, because two recordings of the same work by the same
        # composer produce the same filename -- and shutil.move OVERWRITES
        # an existing destination. On 2026-08-24 that destroyed two files
        # before a UNIQUE constraint aborted the run; they were recoverable
        # only from the USB2 mirror.
        dst = unique_path(dst)
        dst_dir.mkdir(parents=True, exist_ok=True)
        conn.execute("UPDATE archive SET artist=?, file_path=? WHERE id=?",
                     (winner, str(dst), r["id"]))
        shutil.move(str(src), str(dst))
        conn.execute(
            "INSERT INTO events (run_id,event_type,file_path,old_value,new_value,stage,note) "
            "VALUES (?,?,?,?,?,?,?)",
            ("collab-resolve", "COLLAB_CREDIT_RESOLVED", str(dst), r["artist"], winner,
             "manual", "assigned to the most-held component artist; owner rule 2026-08-24"))
        conn.commit()
        conn.commit()
        moved += 1
        for d in (album_dir, artist_dir):
            try:
                d.rmdir()
            except OSError:
                pass
    conn.commit()
    print(f"\napplied: {moved} track(s) reassigned")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
