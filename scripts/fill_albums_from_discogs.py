#!/usr/bin/env python3
"""Second pass at empty albums, using Discogs where AcoustID declined.

AcoustID recovered 1,482 of 2,192 albumless tracks from the audio itself.
It declined the rest for good reasons: 358 had no plain Album among their
release groups, 285 had no release groups at all.

Discogs is text matching -- artist and title -- so it is weaker evidence than
a fingerprint and needs tighter filters. The same discipline applies:

  1. format must contain "Album", and must NOT contain Single, EP,
     Compilation or Unofficial Release. Searching Toto's "Rockmaker" returns
     two singles named after the track before it returns the album that
     carries it.
  2. the artist in the release title must match ours. Discogs titles are
     "Artist - Album", so this is a direct check.
  3. earliest year wins, so an original beats a reissue.
  4. anything still ambiguous is LEFT EMPTY.

Never overwrites an album that already exists. Records every attempt with
its reason, so a decision can be read back rather than guessed at.

Spotify was tried first and is unavailable: the client-credentials token is
issued, but every search returns 403 Forbidden -- the app is in development
mode, which Spotify restricts. That needs a dashboard change, not code.
"""
from __future__ import annotations

import argparse, csv, json, os, re, sqlite3, sys, time, unicodedata
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, "/mnt/FORGE2TB/Projects/MUSAEUS")
from musaeus.brackets import strip_bracketed
from musaeus.config import MusicConfig

SEARCH = "https://api.discogs.com/database/search"
UA = "MUSAEUS/1.0 +local"
RATE_S = 1.1                      # Discogs: 60 requests/minute authenticated
BAD = {"single", "ep", "compilation", "unofficial release", "promo", "sampler"}
PROGRESS = "album_fill_discogs.csv"


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = strip_bracketed(s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def search(artist: str, title: str, key: str, secret: str) -> list[dict]:
    q = {"artist": artist, "track": title, "type": "release", "per_page": 25}
    rq = Request(f"{SEARCH}?{urlencode(q)}",
                 headers={"User-Agent": UA,
                          "Authorization": f"Discogs key={key}, secret={secret}"})
    try:
        with urlopen(rq, timeout=30) as r:
            return json.load(r).get("results", [])
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return []


#: A title carrying one of these describes a SPECIFIC recording, and Discogs
#: track search ignores the qualifier: searching Aerosmith "Mama Kin (Live
#: Version)" returns the 1973 studio debut, which does not carry the live
#: take at all. A fingerprint knows which recording it is holding; text does
#: not. Where the qualifier cannot be honoured, leave the album empty.
VERSION_QUALIFIER = re.compile(
    r"\b(live|unplugged|in concert|remix|re-?recorded|acoustic|demo|"
    r"radio edit|single version|extended|instrumental|karaoke|reprise|"
    r"mono|stereo version|alternate)\b", re.I)


def choose(results: list[dict], artist: str) -> tuple[str | None, str]:
    want = norm(artist).split(" ")[0] if artist else ""
    cands = []
    for x in results:
        fmts = {f.lower() for f in (x.get("format") or [])}
        if "album" not in fmts or (fmts & BAD):
            continue
        title = x.get("title") or ""
        if " - " not in title:
            continue
        who, album = title.split(" - ", 1)
        if want and want not in norm(who):
            continue
        year = x.get("year")
        try:
            year = int(year)
        except (TypeError, ValueError):
            year = None
        cands.append((year or 9999, album.strip()))
    if not cands:
        return None, "no Album release credited to our artist"
    names = {c[1] for c in cands}
    if len(names) == 1:
        return cands[0][1], "one album, unambiguous"
    cands.sort()
    if cands[0][0] == 9999:
        return None, f"{len(names)} candidates, no years to choose on"
    earliest = [c for c in cands if c[0] == cands[0][0]]
    if len({c[1] for c in earliest}) > 1:
        return None, f"{len(names)} candidates, earliest year is a tie"
    return cands[0][1], f"earliest of {len(names)} ({cands[0][0]})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    cfg = MusicConfig.from_env()
    key, secret = os.environ.get("DISCOGS_CONSUMER_KEY"), os.environ.get("DISCOGS_CONSUMER_SECRET")
    if not (key and secret):
        print("Discogs credentials not available", file=sys.stderr)
        return 1

    db = sqlite3.connect(cfg.db_path, timeout=600)
    db.row_factory = sqlite3.Row
    prog = cfg.meta_dir / PROGRESS
    done = {r["file_path"] for r in csv.DictReader(prog.open())} if prog.exists() else set()

    rows = [r for r in db.execute(
        "SELECT id, file_path, artist, title FROM archive "
        " WHERE status='CATALOGUED' AND (album IS NULL OR TRIM(album)='') "
        "   AND artist IS NOT NULL AND TRIM(artist) <> '' "
        "   AND title IS NOT NULL AND TRIM(title) <> '' ORDER BY artist, title")
        if r["file_path"] not in done]
    if args.limit:
        rows = rows[: args.limit]
    print(f"{len(rows):,} to try ({len(done):,} already attempted)", flush=True)
    if not rows:
        return 0

    existed = prog.exists()
    fh = prog.open("a", newline="")
    w = csv.DictWriter(fh, fieldnames=["file_path", "artist", "title", "album", "reason"])
    if not existed:
        w.writeheader()

    filled = empty = 0
    for i, r in enumerate(rows, 1):
        if VERSION_QUALIFIER.search(r["title"] or ""):
            album, reason = None, "title names a specific version; text match cannot honour it"
        else:
            album, reason = choose(search(r["artist"], r["title"], key, secret), r["artist"])
            time.sleep(RATE_S)
        if album and args.apply:
            db.execute("UPDATE archive SET album=? WHERE id=? AND (album IS NULL OR TRIM(album)='')",
                       (album, r["id"]))
        filled += bool(album)
        empty += (not album)
        w.writerow({"file_path": r["file_path"], "artist": r["artist"],
                    "title": r["title"], "album": album or "", "reason": reason})
        if i % 25 == 0:
            fh.flush(); db.commit()
            print(f"  {i:,}/{len(rows):,}  filled {filled:,}  empty {empty:,}", flush=True)
    fh.close(); db.commit()
    print(f"DONE  filled {filled:,}  left empty {empty:,}  (apply={args.apply})")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
