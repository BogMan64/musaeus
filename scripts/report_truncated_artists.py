#!/usr/bin/env python3
"""
Report artist names that look truncated or damaged. Never fixes anything.

TODO item 7. Canon repair already works ("Jan" -> "Jan and Dean"); what did
not exist was spotting a *new* truncation with no canon entry.

The constraint comes from a failed attempt: automated suffix-matching is
untrustworthy in both directions. It flags real distinctions -- "Paul Young"
and "John Paul Young" are different people -- while missing real damage:
"nce Boylan" is not a suffix of anything in the library, because the only
copy of that artist was already broken.

So this does not decide. It gathers independent signals, attaches whatever
MusicBrainz says, and writes a CSV for a human. **It is report-only by
construction: it opens the database read-only.**

Signals, cheapest first:

  lowercase_start   The stored name begins with a lowercase letter and is
                    not a known stylised name. "and the Mysterians" is
                    "? and the Mysterians" with the "?" eaten.
  front_truncated   The stored name appears inside the file's own name but
                    NOT at its start -- the shape truncation actually makes.
                    A disk form that merely starts with the stored name is a
                    collaboration credit ("Santana feat. Rob Thomas") and is
                    deliberately ignored.
  suffix_of         The name is a word-boundary suffix of another artist in
                    the library. Recorded as context, and never enough on its
                    own: "Paul Young" is a suffix of "John Paul Young" and
                    they are different people.
  mb_suggests       MusicBrainz's best match for the name differs from it.
                    The strongest signal available, and the reason --confirm
                    exists, but still only evidence.

Usage:
    python3 scripts/report_truncated_artists.py
    python3 scripts/report_truncated_artists.py --confirm      # adds MB lookups
    python3 scripts/report_truncated_artists.py --confirm --limit 40
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.stages.normalize import PROTECTED_ARTIST_NAMES  # noqa: E402

VAULT = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT")
ARTIST_CANON = VAULT / "MetaData" / "artist_canon.tsv"
DB = VAULT / "musaeus.db"
OUT = Path.home() / "Desktop" / "MUSAEUS_Truncated_Artist_Report.csv"

# Names that are legitimately lowercase or punctuation-led. PROTECTED_ARTIST_NAMES
# covers the ones normalize.py already defends; these are the rest this library
# actually holds, confirmed by eye 2026-08-23.
_KNOWN_STYLISED = {
    "a-ha",
    "blink-182",
    "twenty one pilots",
    "fun.",
    "emf",
    "w h i t e r o o m",
    "monkey bgm",
    "american poetry club",
}

_ARTIST_SEGMENT_RE = re.compile(r"^(.*?) - ")


def _stored_artists(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT artist, COUNT(*) AS n FROM archive "
        "WHERE status='CATALOGUED' AND artist IS NOT NULL AND TRIM(artist) != '' "
        "GROUP BY artist"
    ).fetchall()
    return {r["artist"]: r["n"] for r in rows}


def _filename_artist(file_path: str) -> str:
    """The artist segment of "<Artist> - <Title>.ext", or ""."""
    m = _ARTIST_SEGMENT_RE.match(Path(file_path).name)
    return m.group(1).strip() if m else ""


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def load_artist_canon() -> dict[str, str]:
    """raw name (lowercased) → canonical name, from artist_canon.tsv."""
    if not ARTIST_CANON.exists():
        return {}
    out: dict[str, str] = {}
    for line in ARTIST_CANON.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "\t" not in line:
            continue
        raw, canonical = line.split("\t", 1)
        out[raw.strip().lower()] = canonical.strip()
    return out


def _is_stylised(artist: str) -> bool:
    low = artist.strip().lower()
    return low in PROTECTED_ARTIST_NAMES or low in _KNOWN_STYLISED


def collect(conn: sqlite3.Connection) -> dict[str, dict]:
    artists = _stored_artists(conn)
    canon = load_artist_canon()
    findings: dict[str, dict] = defaultdict(
        lambda: {"tracks": 0, "signals": set(), "filename_form": "", "suffix_of": ""}
    )

    # 1. lowercase / punctuation-led starts
    for artist, n in artists.items():
        if artist[:1].islower() and not _is_stylised(artist):
            f = findings[artist]
            f["tracks"] = n
            f["signals"].add("lowercase_start")

    # 2. the file's own name disagrees with the stored artist
    rows = conn.execute(
        "SELECT artist, file_path FROM archive WHERE status='CATALOGUED' "
        "AND artist IS NOT NULL AND TRIM(artist) != ''"
    ).fetchall()
    for r in rows:
        on_disk = _filename_artist(r["file_path"])
        if not on_disk:
            continue
        stored = r["artist"].strip()
        if _norm(on_disk) == _norm(stored):
            continue
        # Truncation eats the FRONT of a name. When the disk form merely
        # starts with the stored name, the extra text is a collaboration
        # credit -- "Santana" on disk as "Santana feat. Rob Thomas" -- and
        # flagging it reproduces exactly the false-positive problem that
        # sank the first attempt at this. Only a stored name appearing
        # somewhere OTHER than the start counts.
        ns, nd = _norm(stored), _norm(on_disk)
        # A file whose on-disk name maps to the stored artist through the
        # artist canon is a rename that has not been relocated yet -- not
        # damage. After the 2026-08-24 consolidation, "Dr. Dre" sat in a
        # file still called "2Pac, Roger Troutman, Dr. Dre - ...", which
        # this rule read as a front-truncation. The canon is the record of
        # deliberate renames, so it is what tells the two apart.
        if canon.get(on_disk.strip().lower(), "").strip().lower() == stored.lower():
            continue
        if ns and ns in nd and not nd.startswith(ns):
            f = findings[stored]
            f["tracks"] = artists.get(stored, 0)
            f["signals"].add("front_truncated")
            f["filename_form"] = on_disk

    # 3. word-boundary suffix of another library artist
    by_norm = {a: _norm(a) for a in artists}
    for short, ns in by_norm.items():
        if len(short) < 4:
            continue
        for long_, nl in by_norm.items():
            if short is long_ or len(nl) <= len(ns):
                continue
            if nl.endswith(ns) and re.search(rf"(^|\s){re.escape(short)}$", long_, re.I):
                f = findings[short]
                f["tracks"] = artists[short]
                f["signals"].add("suffix_of")
                f["suffix_of"] = long_
                break
    # suffix_of is recorded as context but never qualifies a candidate by
    # itself: "Paul Young" is a word-boundary suffix of "John Paul Young"
    # and they are different people. A row survives only on front_truncated
    # or lowercase_start -- signals that point at damage rather than at
    # coincidence.
    qualifying = {"lowercase_start", "front_truncated"}
    return {a: f for a, f in findings.items() if f["signals"] & qualifying}


def confirm(findings: dict[str, dict], limit: int) -> None:
    """Attach MusicBrainz's best match. Evidence only -- nothing is applied."""
    from musaeus.network_policy import NetworkPolicy, policy
    from musaeus.stages.mb_enrich import _search_artist

    # Scoped, not set_policy: the gateway is module-level mutable state, so
    # granting ALLOWED and not restoring it leaves everything afterwards in
    # the process permissive. This function is one bounded piece of work,
    # not a mode the whole process should stay in.
    with policy(NetworkPolicy.ALLOWED):
        _confirm_inner(findings, limit, _search_artist)


def _confirm_inner(findings: dict[str, dict], limit: int, _search_artist) -> None:
    for i, (artist, f) in enumerate(sorted(findings.items())):
        if limit and i >= limit:
            break
        try:
            hit = _search_artist(artist)
        except Exception as exc:  # noqa: BLE001 - a lookup failure is not fatal
            f["mb_name"] = f"lookup error: {exc}"
            continue
        if hit and _norm(hit[1]) != _norm(artist):
            f["mb_name"] = hit[1]
            f["signals"].add("mb_suggests")
        elif hit:
            f["mb_name"] = hit[1]
        else:
            f["mb_name"] = "(no match)"
        time.sleep(1.2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true", help="add MusicBrainz lookups")
    ap.add_argument("--limit", type=int, default=0, help="cap MB lookups")
    args = ap.parse_args()

    # Read-only by construction: this script cannot write to the library even
    # if a later edit tries to.
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    findings = collect(conn)
    print(f"candidates: {len(findings)}")

    if args.confirm:
        print("confirming against MusicBrainz…")
        confirm(findings, args.limit)

    rows = sorted(
        findings.items(),
        key=lambda kv: (-len(kv[1]["signals"]), -kv[1]["tracks"], kv[0]),
    )
    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["artist", "tracks", "signals", "filename_form", "suffix_of", "musicbrainz_says"]
        )
        for artist, f in rows:
            w.writerow(
                [
                    artist,
                    f["tracks"],
                    "+".join(sorted(f["signals"])),
                    f["filename_form"],
                    f["suffix_of"],
                    f.get("mb_name", ""),
                ]
            )

    print(f"\n{'artist':<32} {'trk':>4}  signals")
    print("-" * 78)
    for artist, f in rows[:20]:
        print(f"{artist[:31]:<32} {f['tracks']:>4}  {' + '.join(sorted(f['signals']))}")
    print(f"\nwritten: {OUT}")
    print("REPORT ONLY — nothing was changed. Every row needs a human decision.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
