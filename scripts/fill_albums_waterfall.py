#!/usr/bin/env python3
"""Third pass at empty albums: try each streaming source, fall through on failure.

Order is by strength of evidence, not convenience:

  Deezer   public search, no authentication of any kind. Works today.
  Tidal    requires OAuth; no credentials configured, so it reports
           unavailable rather than pretending.
  Spotify  a client-credentials token IS issued, but every search returns
           403 -- the developer app is in development mode, which Spotify
           restricts. Not an account-tier problem: a paid listener account
           would not change it. Needs a dashboard change.

A source that cannot answer is skipped and the next is tried. A source that
answers badly is worse than one that does not answer, so all of them share
the same filters as the Discogs pass:

  - the album must not be a compilation, live album, greatest hits or single
  - the artist must match ours
  - a title naming a specific version (live, remix, acoustic, demo) is not
    attempted at all: the searches ignore the qualifier and will happily
    return the studio album for a live take
  - anything ambiguous is LEFT EMPTY

Never overwrites an album that already exists.
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

UA = "MUSAEUS/1.0 +local"
PROGRESS = "album_fill_waterfall.csv"

VERSION_QUALIFIER = re.compile(
    r"\b(live|unplugged|in concert|remix|re-?recorded|acoustic|demo|"
    r"radio edit|single version|extended|instrumental|karaoke|reprise|"
    r"mono|alternate)\b", re.I)

BAD_ALBUM = re.compile(
    r"\b(greatest hits|best of|the collection|essential|anthology|"
    r"compilation|live (at|in|from)|hits|vol\.? ?\d|now that|"
    r"ultimate|definitive|platinum collection|super hits)\b", re.I)


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = strip_bracketed(s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ── Deezer: public, no auth ────────────────────────────────────────────
def deezer(artist: str, title: str) -> tuple[str | None, str]:
    try:
        url = f"https://api.deezer.com/search?{urlencode({'q': f'{artist} {title}', 'limit': 10})}"
        with urlopen(Request(url, headers={"User-Agent": UA}), timeout=20) as r:
            items = json.load(r).get("data", [])
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return None, f"deezer unavailable: {exc}"
    want_a, want_t = norm(artist), norm(title)
    names: list[str] = []
    for t in items:
        if norm(t.get("artist", {}).get("name")) != want_a:
            continue
        if norm(t.get("title")) != want_t:
            continue
        alb = (t.get("album") or {}).get("title") or ""
        if not alb or BAD_ALBUM.search(alb) or norm(alb) == want_t:
            continue                      # skip compilations and single-named-for-track
        names.append(alb)
    if not names:
        return None, "deezer: no studio album for an exact artist+title match"
    uniq = list(dict.fromkeys(names))
    if len(uniq) == 1:
        return uniq[0], "deezer: unambiguous"
    return None, f"deezer: {len(uniq)} candidate albums"


def tidal(artist: str, title: str) -> tuple[str | None, str]:
    if not (os.environ.get("TIDAL_CLIENT_ID") and os.environ.get("TIDAL_CLIENT_SECRET")):
        return None, "tidal: no credentials configured"
    return None, "tidal: not implemented"


def spotify(artist: str, title: str) -> tuple[str | None, str]:
    return None, "spotify: app in development mode, search returns 403"


SOURCES = [("deezer", deezer), ("tidal", tidal), ("spotify", spotify)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    cfg = MusicConfig.from_env()
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
    w = csv.DictWriter(fh, fieldnames=["file_path", "artist", "title", "album", "source", "reason"])
    if not existed:
        w.writeheader()

    filled = empty = 0
    for i, r in enumerate(rows, 1):
        album = source = None
        reasons = []
        if VERSION_QUALIFIER.search(r["title"] or ""):
            reasons = ["title names a specific version; text match cannot honour it"]
        else:
            for name, fn in SOURCES:
                album, why = fn(r["artist"], r["title"])
                reasons.append(why)
                if album:
                    source = name
                    break
                time.sleep(0.25)
        if album and args.apply:
            db.execute("UPDATE archive SET album=? WHERE id=? AND (album IS NULL OR TRIM(album)='')",
                       (album, r["id"]))
        filled += bool(album); empty += (not album)
        w.writerow({"file_path": r["file_path"], "artist": r["artist"], "title": r["title"],
                    "album": album or "", "source": source or "", "reason": " | ".join(reasons)[:200]})
        if i % 25 == 0:
            fh.flush(); db.commit()
            print(f"  {i:,}/{len(rows):,}  filled {filled:,}  empty {empty:,}", flush=True)
    fh.close(); db.commit()
    print(f"DONE  filled {filled:,}  left empty {empty:,}  (apply={args.apply})")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
