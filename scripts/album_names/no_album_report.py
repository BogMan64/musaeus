#!/usr/bin/env python3
"""Two reports on tracks with no album name: one to work from, one to hand out.

WHY TWO FILES
-------------
They answer different questions and the same file cannot do both.

  missing_albums.csv   the working copy. Carries archive_id and file_path so
                       an answer can be applied to the right row, plus the
                       classical columns, because Grey files classical under
                       the COMPOSER rather than the album.

  ..._for_research.csv the copy that leaves the machine. Artist, title, year,
                       duration and a blank column to fill -- no paths, no
                       ids, nothing that only means something here. A chat
                       window cannot use a file path and pasting one is just
                       noise.

COMPOSER, FOR THE CLASSICAL ROWS
--------------------------------
The classical recordings are credited to PERFORMERS -- Danielle De Niese,
Nigel Kennedy, the Zagreb Soloists -- so matching the artist against
Composer_Canon.tsv finds nothing, which is why that column came back empty on
exactly the rows it existed for. Three sources are tried, cheapest first:

  1. the file's own composer tag
  2. a thematic CATALOGUE number in the title -- BWV is Bach, K./KV is
     Mozart's Kochel, D. is Schubert's Deutsch, Hob. is Haydn. A catalogue is
     by definition one composer's, so this is a fact rather than a guess.
  3. a named WORK so strongly attached to one composer that the title alone
     settles it: Canon in D is Pachelbel's, the Nutcracker is Tchaikovsky's.
     Kept deliberately short -- this list earns its place only while every
     entry is unambiguous.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from musaeus.config import MusicConfig  # noqa: E402

# \d+ not \d, because the group carries a trailing \b: "no\.\s?\d\b" matches
# "No. 8" and FAILS on "No. 36" -- the \d takes the 3 and the 6 is not a word
# boundary. That silently left multi-digit movement numbers unflagged, which
# is most of them. Compiles, lints, and lies.
CLASSICAL_HINT = re.compile(
    r"\b(op\.|opus|symphony|concerto|sonata|quartet|quintet|nocturne|prelude|fugue|"
    r"adagio|allegro|andante|BWV|K\.\s?\d+|no\.\s?\d+|major|minor|mass|requiem|"
    r"cantata|oratorio|aria|overture|etude|waltz|rhapsody)\b", re.I)

# A thematic catalogue belongs to exactly one composer. That is what makes
# this a lookup and not a guess.
CATALOGUE = [
    (re.compile(r"\bBWV\s?\d", re.I), "Johann Sebastian Bach"),
    (re.compile(r"\bK(?:V|\.)\s?\d", re.I), "Wolfgang Amadeus Mozart"),
    (re.compile(r"\bD\.\s?\d{2,}", re.I), "Franz Schubert"),
    (re.compile(r"\bHob\.\s?[IVX]", re.I), "Joseph Haydn"),
    (re.compile(r"\bRV\s?\d", re.I), "Antonio Vivaldi"),
    (re.compile(r"\bZ\.\s?\d{2,}", re.I), "Henry Purcell"),
    (re.compile(r"\bHWV\s?\d", re.I), "George Frideric Handel"),
]

# Works whose title alone is unambiguous. Short on purpose.
WORKS = [
    (re.compile(r"\bcanon (?:and gigue )?in d\b", re.I), "Johann Pachelbel"),
    (re.compile(r"\bnutcracker\b", re.I), "Pyotr Ilyich Tchaikovsky"),
    (re.compile(r"\bswan lake\b", re.I), "Pyotr Ilyich Tchaikovsky"),
    (re.compile(r"\b1812 overture\b", re.I), "Pyotr Ilyich Tchaikovsky"),
    (re.compile(r"\bbolero\b", re.I), "Maurice Ravel"),
    (re.compile(r"\bclair de lune\b", re.I), "Claude Debussy"),
    (re.compile(r"\bf[uü]r elise\b", re.I), "Ludwig van Beethoven"),
    (re.compile(r"\bode to joy\b", re.I), "Ludwig van Beethoven"),
    (re.compile(r"\bmoonlight sonata\b", re.I), "Ludwig van Beethoven"),
    (re.compile(r"\bmessiah\b", re.I), "George Frideric Handel"),
    (re.compile(r"\bfour seasons\b", re.I), "Antonio Vivaldi"),
    (re.compile(r"\bring of the nibelung|ride of the valkyries\b", re.I), "Richard Wagner"),
    (re.compile(r"\bpeer gynt|hall of the mountain king\b", re.I), "Edvard Grieg"),
    (re.compile(r"\bcarmina burana\b", re.I), "Carl Orff"),
    (re.compile(r"\brhapsody in blue\b", re.I), "George Gershwin"),
]


def load_canon(meta: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    p = meta / "Composer_Canon.tsv"
    if not p.exists():
        return out
    for ln in p.read_text(encoding="utf-8").splitlines():
        if ln.strip() and not ln.startswith("#"):
            parts = ln.split("\t")
            if len(parts) >= 2:
                out[parts[0].strip()] = parts[1].strip()
    return out


def composer_from_tag(fp: str) -> str:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", fp],
            capture_output=True, text=True, timeout=15)
        tags = {k.lower(): v for k, v in
                json.loads(r.stdout).get("format", {}).get("tags", {}).items()}
    except Exception:
        return ""
    return (tags.get("composer") or tags.get("wrt") or "").strip()


def composer_from_text(canon: dict[str, str], *texts: str) -> str:
    variants = sorted(canon, key=len, reverse=True)   # "J.S. Bach" before "Bach"
    for t in texts:
        if not t:
            continue
        for rx, who in CATALOGUE:
            if rx.search(t):
                return who
        for rx, who in WORKS:
            if rx.search(t):
                return who
        for v in variants:
            if re.search(rf"(?<!\w){re.escape(v)}(?!\w)", t, re.I):
                return canon[v]
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", type=Path, default=Path.home() / "Desktop")
    args = ap.parse_args()

    cfg = MusicConfig.from_env()
    canon = load_canon(Path(cfg.meta_dir))
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT id, artist, title, genre, year, duration, file_path "
        "FROM archive WHERE status='CATALOGUED' AND COALESCE(album,'')='' "
        "ORDER BY COALESCE(artist,''), COALESCE(title,'')").fetchall()
    conn.close()

    working = args.out_dir / "missing_albums.csv"
    research = args.out_dir / "missing_albums_for_research.csv"
    n_class = n_comp = 0

    with working.open("w", newline="", encoding="utf-8") as wf, \
         research.open("w", newline="", encoding="utf-8") as rf:
        w = csv.writer(wf)
        r = csv.writer(rf)
        w.writerow(["your_album_here", "likely_classical", "composer", "composer_source",
                    "artist", "title", "genre", "year", "duration_s", "archive_id", "file_path"])
        r.writerow(["album_please", "artist", "title", "year", "duration_mmss", "notes"])

        for aid, artist, title, genre, year, dur, fp in rows:
            artist, title = artist or "", title or ""
            is_class = (genre or "").strip().lower() == "classical"
            hint = bool(CLASSICAL_HINT.search(title))
            by_artist = composer_from_text(canon, artist)
            flag = "YES" if (is_class or by_artist or (hint and not genre)) else ("maybe" if hint else "")

            comp = src = ""
            if flag:
                n_class += 1
                comp = composer_from_tag(fp)
                src = "file tag" if comp else ""
                if not comp:
                    comp = composer_from_text(canon, title, artist, fp)
                    src = "title/catalogue" if comp else ""
                if comp:
                    n_comp += 1

            w.writerow(["", flag, comp, src, artist, title, genre or "", year or "",
                        round(dur) if dur else "", aid, fp])

            mmss = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else ""
            note = f"classical - composer {comp}" if comp else ("classical" if flag else "")
            r.writerow(["", artist, title, year or "", mmss, note])

    print(f"{len(rows)} track(s) with no album")
    print(f"  classical flagged : {n_class}   with a composer: {n_comp}")
    print(f"  -> {working}")
    print(f"  -> {research}   (artist/title only -- safe to paste into a chat)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
