#!/usr/bin/env python3
"""
MUSAEUS — iTunes Genre Fill

Fills MasterLaw.csv for artists no authority has an opinion about, by asking
the iTunes Search API what genre they are.

Ported in concept from ORPHEUS's orpheus_masterlaw_itunes_fill.py, which
MUSAEUS never had. Grey paid for that absence by hand on 2026-09-06/07:
41 artists, a review CSV, a ruling on each, then writing them into
MasterLaw one at a time. This does the mechanical part of that.

What it does NOT do
-------------------
It does not decide whether an artist belongs in the library. iTunes will
happily return a genre for "Party Tyme" and "Stephen Mc-Queen"; both are
knock-offs Grey deleted. Deciding that is what ArtistsToReview.csv is for,
and this script is deliberately downstream of it: it answers "what genre is
this artist", never "should this artist be here".

Confidence
----------
HIGH   the artist name iTunes returned matches the one we asked about, and
       its genre maps to a genre that is currently in Genre_Allowed.txt.
       Written straight to MasterLaw.csv.
LOW    anything else -- a different artist came back, no result, or the
       genre maps to nothing (or to a RETIRED genre; "Baroque" is still a
       target in Genre_Canonical_Map.txt but was retired from the
       vocabulary on 2026-09-07). Written to a review CSV for Grey.

Network
-------
Every request goes through musaeus.network_policy first, so a preview or an
unattended run cannot reach out. --apply grants access the same way the
pipeline's execute path does. Requests are throttled; the API is
unauthenticated and rate-limits around 20/minute.

Usage:
    python3 scripts/itunes_genre_fill.py            # preview, no network
    python3 scripts/itunes_genre_fill.py --apply    # query and write
    python3 scripts/itunes_genre_fill.py --apply --limit 20
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.canon.genre_law import GenreLaw  # noqa: E402
from musaeus.config import MusicConfig  # noqa: E402
from musaeus.network_policy import NetworkPolicy, check as network_check, set_policy  # noqa: E402

_API = "https://itunes.apple.com/search"
_THROTTLE_S = 3.5  # ~17/min, under the unauthenticated limit


def fold(name: str) -> str:
    """Article-folding key. GenreLaw's, imported not reimplemented -- both
    sides of a comparison must fold identically or "Byrds, The" reads as
    unknown while "The Byrds" sits in the law."""
    return GenreLaw._key(name or "")


def allowed_genres(cfg: MusicConfig) -> set[str]:
    p = cfg.meta_dir / "Genre_Allowed.txt"
    return {ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")}


def genre_map(cfg: MusicConfig) -> dict[str, str]:
    """raw genre -> canonical genre, from Genre_Canonical_Map.txt."""
    out: dict[str, str] = {}
    p = cfg.meta_dir / "Genre_Canonical_Map.txt"
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        if "=>" not in line or line.startswith("#"):
            continue
        raw, canon = line.split("=>", 1)
        out[raw.strip().lower()] = canon.strip()
    return out


# Genres that are almost never right for a music artist. iTunes returned
# "Holiday" for a Blues Brothers covers act on the first live run, and the
# high-confidence test happily wrote it, because "Holiday" IS in the
# vocabulary. Rare genres are exactly where a wrong answer hides.
SUSPICIOUS = frozenset({"Holiday", "Sports", "Comedy", "Children's Music",
                        "Spoken Word", "Karaoke", "Tribute", "Hypnotherapy"})


def library_genre(cfg: MusicConfig, artist: str) -> str | None:
    """The genre the library ALREADY agrees on for this artist, if it does.

    Checked before asking iTunes, and it is not an optimisation -- it is a
    correctness fix. The first live run wrote "Tiny Bradshaw,Pop" into
    MasterLaw while his one catalogued track had said Jazz all along. That
    is two authorities disagreeing, created by the very script meant to
    settle them. The library's own tagging is better evidence about Grey's
    library than a search API is, and it costs no request.
    """
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    rows = [g for (g,) in conn.execute(
        "SELECT DISTINCT genre FROM archive WHERE artist=? AND status='CATALOGUED' "
        "AND genre IS NOT NULL AND trim(genre)!=''", (artist,))]
    conn.close()
    return rows[0] if len(rows) == 1 else None


def unknown_artists(cfg: MusicConfig) -> list[tuple[str, int]]:
    """Artists in the library that MasterLaw has never heard of."""
    law = cfg.meta_dir / "MasterLaw.csv"
    known = set()
    if law.exists():
        with law.open(encoding="utf-8") as fh:
            known = {fold(r[0]) for r in csv.reader(fh) if r and r[0].strip()}
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT artist, COUNT(*) FROM archive WHERE status='CATALOGUED' "
        "AND artist IS NOT NULL AND trim(artist)!='' GROUP BY artist"
    ).fetchall()
    conn.close()
    return sorted(((a, n) for a, n in rows if fold(a) not in known),
                  key=lambda x: -x[1])


def ask_itunes(artist: str, timeout: int = 20) -> tuple[str, str] | None:
    """(artistName, primaryGenreName) for the best match, or None."""
    url = "%s?%s" % (_API, urlencode(
        {"term": artist, "entity": "musicArtist", "limit": 5}))
    network_check(url)
    with urlopen(url, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    for r in data.get("results", []):
        if fold(r.get("artistName", "")) == fold(artist):
            return r.get("artistName", ""), r.get("primaryGenreName", "")
    results = data.get("results", [])
    if results:
        return results[0].get("artistName", ""), results[0].get("primaryGenreName", "")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="query iTunes and write; without it nothing leaves the machine")
    ap.add_argument("--limit", type=int, default=0, help="stop after N artists")
    args = ap.parse_args()

    cfg = MusicConfig.from_env()
    allowed = allowed_genres(cfg)
    gmap = genre_map(cfg)
    todo = unknown_artists(cfg)
    if args.limit:
        todo = todo[:args.limit]

    if not todo:
        print("Every catalogued artist already has a MasterLaw entry. Nothing to do.")
        return 0
    print("%d artist(s) unknown to MasterLaw" % len(todo))
    if not args.apply:
        for a, n in todo[:20]:
            print("   %4dt  %s" % (n, a))
        print("\nPREVIEW ONLY — no network request was made. Re-run with --apply.")
        return 0

    set_policy(NetworkPolicy.ALLOWED)
    high, low = [], []
    for i, (artist, tracks) in enumerate(todo, 1):
        # The library first. If its own tracks already agree on a genre,
        # that settles it and no request is made.
        own = library_genre(cfg, artist)
        if own and own in allowed:
            high.append({"artist": artist, "genre": own, "tracks": tracks,
                         "itunes_genre": "(not asked -- the library already agreed)"})
            print("  [%d/%d] %-34s HIGH (from the library)" % (i, len(todo), artist[:34]))
            continue
        try:
            got = ask_itunes(artist)
        except Exception as exc:                       # noqa: BLE001
            low.append({"artist": artist, "tracks": tracks, "itunes_artist": "",
                        "itunes_genre": "", "mapped": "",
                        "why": "lookup failed: %s" % exc})
            got = None
        if got:
            name, raw = got
            mapped = gmap.get((raw or "").lower(), raw or "")
            exact = fold(name) == fold(artist)
            if exact and mapped in SUSPICIOUS:
                low.append({"artist": artist, "tracks": tracks, "itunes_artist": name,
                            "itunes_genre": raw, "mapped": mapped,
                            "why": "%r is rarely right for a music artist" % mapped})
            elif exact and mapped in allowed:
                high.append({"artist": artist, "genre": mapped, "tracks": tracks,
                             "itunes_genre": raw})
            else:
                low.append({"artist": artist, "tracks": tracks, "itunes_artist": name,
                            "itunes_genre": raw, "mapped": mapped,
                            "why": "iTunes returned a different artist" if not exact
                                   else "%r is not in Genre_Allowed.txt" % mapped})
        elif not any(r["artist"] == artist for r in low):
            low.append({"artist": artist, "tracks": tracks, "itunes_artist": "",
                        "itunes_genre": "", "mapped": "", "why": "no result"})
        print("  [%d/%d] %-34s %s" % (i, len(todo), artist[:34],
                                      "HIGH" if high and high[-1]["artist"] == artist
                                      else "low"))
        if i < len(todo):
            time.sleep(_THROTTLE_S)

    if high:
        law = cfg.meta_dir / "MasterLaw.csv"
        with law.open("a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows([[r["artist"], r["genre"]] for r in high])
    if low:
        out = cfg.alac_archive / "ArtistGenreReview.csv"
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["GENRE? (type one)", "artist", "tracks",
                                               "itunes_artist", "itunes_genre",
                                               "mapped", "why"])
            w.writeheader()
            for r in low:
                w.writerow({"GENRE? (type one)": "", **r})

    print("\nHIGH confidence, written to MasterLaw.csv : %d" % len(high))
    for r in high:
        print("   %-34s %-18s (iTunes said %r)" % (r["artist"][:34], r["genre"],
                                                   r["itunes_genre"]))
    print("LOW confidence, for review                : %d" % len(low))
    for r in low:
        print("   %-34s %s" % (r["artist"][:34], r["why"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
