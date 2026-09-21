#!/usr/bin/env python3
"""Fill empty album names from AcoustID release groups, conservatively.

Why not mb_enrich
-----------------
`mb_enrich` looks a release up by artist MBID **plus album title**, and
returns immediately when there is no album name. It needs an album to find an
album. Measured 2026-09-21: mb_release_id was present on 0 of 10,881 rows and
2,190 of 2,192 albumless tracks had already been through mb_enrich.

Fingerprinting needs no existing tags, which is the property required here.

The selection rule, and why each filter exists
----------------------------------------------
1. type == "Album" with no secondary types.
   Compilations dominate the raw candidate list. One track returned 64
   release groups of which 61 were compilations; the filter left exactly 1,
   the correct original album.
2. The release group's artist must match ours.
   AcoustID returned "God's Property" (a gospel album) among the candidates
   for The Who's "Bargain". Dates alone would not have excluded it.
3. Earliest MusicBrainz first-release-date wins.
   AcoustID release groups carry no date, so a remaster or reissue is
   indistinguishable from the original without asking MusicBrainz. "Bargain"
   resolves to Who's Next (1971-08-14) over Face Dances (1981-03-16).

Anything still ambiguous is LEFT EMPTY. A confidently wrong album is worse
than a missing one: it looks authoritative, it moves the file into a wrong
folder, and nothing downstream can tell it was a guess.

Never overwrites an album that already exists.

A note on the API
-----------------
meta values must NOT be joined with "+" through urlencode: it escapes to %2B,
AcoustID does not recognise the value, and answers `status: ok` with neither
recordings nor releasegroups. A malformed query that returns success is
indistinguishable from a definitive negative -- it read as "0 of 2,192
fillable" until a control test on a known track exposed it. Ask for one meta
value at a time.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, "/mnt/FORGE2TB/Projects/MUSAEUS")
from musaeus.config import MusicConfig

ACOUSTID = "https://api.acoustid.org/v2/lookup"
MB_RG = "https://musicbrainz.org/ws/2/release-group/{}?fmt=json"
UA = "MUSAEUS/1.0 ( musaeus-local )"
MB_RATE_S = 1.1          # MusicBrainz asks for 1 request/second
AID_RATE_S = 0.35
PROGRESS = "album_fill_progress.csv"


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def fingerprint(path: Path) -> tuple[str, int] | None:
    try:
        out = subprocess.run(["fpcalc", "-json", str(path)],
                             capture_output=True, text=True, timeout=120).stdout
        d = json.loads(out)
        return d["fingerprint"], int(d["duration"])
    except Exception:
        return None


def acoustid_groups(fp: str, dur: int, key: str) -> list[dict]:
    params = {"client": key, "fingerprint": fp, "duration": str(dur),
              "meta": "releasegroups", "format": "json"}
    try:
        with urlopen(f"{ACOUSTID}?{urlencode(params)}", timeout=45) as r:
            data = json.load(r)
    except (URLError, TimeoutError, json.JSONDecodeError):
        return []
    results = sorted(data.get("results") or [], key=lambda x: x.get("score", 0), reverse=True)
    return (results[0].get("releasegroups") or []) if results else []


def mb_first_release(mbid: str) -> str | None:
    try:
        with urlopen(Request(MB_RG.format(mbid), headers={"User-Agent": UA}), timeout=30) as r:
            return (json.load(r).get("first-release-date") or None)
    except Exception:
        return None


def choose(groups: list[dict], artist: str) -> tuple[str | None, str]:
    """Return (album_title, reason). album_title is None when unsure."""
    clean = [g for g in groups
             if g.get("type") == "Album" and not g.get("secondarytypes")]
    if not clean:
        return None, "no plain Album among candidates"

    want = norm(artist).split(" ")[0] if artist else ""
    if want:
        matched = [g for g in clean
                   if any(want in norm(a.get("name")) for a in (g.get("artists") or []))]
        if matched:
            clean = matched
        elif len(clean) > 1:
            return None, "no candidate credits our artist"

    if len(clean) == 1:
        return clean[0].get("title"), "single clean Album"

    dated = []
    for g in clean[:6]:                      # cap MB calls per track
        d = mb_first_release(g["id"])
        time.sleep(MB_RATE_S)
        if d:
            dated.append((d, g.get("title")))
    if not dated:
        return None, f"{len(clean)} candidates, no release dates"
    dated.sort()
    if len(dated) > 1 and dated[0][0] == dated[1][0]:
        return None, "earliest date is a tie"
    return dated[0][1], f"earliest of {len(dated)} ({dated[0][0]})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--apply", action="store_true", help="write albums to the DB")
    args = ap.parse_args()

    cfg = MusicConfig.from_env()
    key = cfg.acousticid_api_key
    if not key:
        print("ACOUSTICID_API_KEY not available", file=sys.stderr)
        return 1

    db = sqlite3.connect(cfg.db_path, timeout=600)
    db.row_factory = sqlite3.Row
    progress = cfg.meta_dir / PROGRESS
    done: set[str] = set()
    if progress.exists():
        with progress.open() as fh:
            done = {r["file_path"] for r in csv.DictReader(fh)}

    rows = [r for r in db.execute(
        "SELECT id, file_path, artist, title FROM archive "
        " WHERE status='CATALOGUED' AND (album IS NULL OR TRIM(album)='') "
        "   AND file_path IS NOT NULL ORDER BY artist, title")
        if r["file_path"] not in done]
    if args.limit:
        rows = rows[: args.limit]
    print(f"{len(rows):,} to try ({len(done):,} already attempted)", flush=True)

    new = progress.exists()
    fh = progress.open("a", newline="")
    w = csv.DictWriter(fh, fieldnames=["file_path", "artist", "title", "album", "reason"])
    if not new:
        w.writeheader()

    filled = skipped = 0
    for i, r in enumerate(rows, 1):
        p = Path(r["file_path"])
        album, reason = None, "file missing"
        if p.is_file():
            fp = fingerprint(p)
            if fp is None:
                reason = "fpcalc failed"
            else:
                groups = acoustid_groups(fp[0], fp[1], key)
                time.sleep(AID_RATE_S)
                if not groups:
                    reason = "no AcoustID release groups"
                else:
                    album, reason = choose(groups, r["artist"] or "")
        if album and args.apply:
            db.execute("UPDATE archive SET album=? WHERE id=? AND (album IS NULL OR TRIM(album)='')",
                       (album, r["id"]))
        if album:
            filled += 1
        else:
            skipped += 1
        w.writerow({"file_path": r["file_path"], "artist": r["artist"],
                    "title": r["title"], "album": album or "", "reason": reason})
        if i % 25 == 0:
            fh.flush()
            db.commit()
            print(f"  {i:,}/{len(rows):,}  filled {filled:,}  left empty {skipped:,}", flush=True)
    fh.close()
    db.commit()
    print(f"DONE  filled {filled:,}  left empty {skipped:,}  (apply={args.apply})")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
