#!/usr/bin/env python3
"""Find the live recordings in the library and say which ones need a studio version found.

Grey: "I rarely like a live version over a studio." So this produces a hunting
list -- an M3U you can play through and a CSV you can rule on.

READ-ONLY. The vault database is opened with mode=ro and no file in the music
library is touched, moved or retagged. This tool reports; you decide.

THE ONE DESIGN DECISION WORTH KNOWING ABOUT
-------------------------------------------
MUSAEUS already has a rule for "is this a live recording": _LIVE_RE in
musaeus/stages/dupe_resolver.py, used to make live copies lose to studio copies
when picking a duplicate keeper. This tool does NOT write a second live-detection
rule. It reads _LIVE_MARKERS out of the MUSAEUS source with ast and rebuilds the
identical regex, so the playlist can never disagree with what the deduper thinks.
If that tuple is renamed or stops being a plain literal, this exits with an error
rather than quietly falling back to a private copy -- two subtly different
answers to the same question is the failure this avoids.

But that regex includes the bare word "live", because for keeper-picking a false
positive is nearly free: it only nudges an ordering. For a HUNTING list a false
positive is expensive -- it sends you looking for the studio version of
"Live and Let Die", "Live Wire" or "Live Forever", all of which are studio
recordings whose titles happen to contain the word. So results are tiered:

  CERTAIN   the marker is unambiguous: "Live at ...", "(Live)", "Unplugged",
            "in concert", "BBC session". Hunt these.
  LIKELY    an album-level signal only -- the track title is clean but the
            album says live. Usually right, and it is how The Last Waltz and
            Before the Flood get caught.
  DOUBTFUL  bare "live" appearing as an ordinary word in a title. Probably a
            studio track. Listed separately so you can dismiss the lot quickly
            instead of finding them mixed into the real list.

Only CERTAIN and LIKELY go in the M3U. All three go in the CSV, tagged, because
a hunting list you cannot trust gets abandoned.

The other column that matters is HAVE_STUDIO. If a live track already has a
studio sibling in the library, you are done -- nothing to hunt. Those are
separated out so the M3U leads with the tracks where live is the ONLY copy you
own, which is the actual work.
"""

from __future__ import annotations

import argparse
import ast
import csv
import os
import re

from musaeus.brackets import CLOSE, OPEN
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

VAULT_DB = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/musaeus.db")
# Resolved from THIS file's location, not from a hardcoded checkout path.
# Until 2026-09-15 this named /mnt/FORGE2TB/Projects/MUSAEUS-sandbox, a
# directory that does not exist, so the tool could not run at all -- it exited
# with its own "point --musaeus-src at a MUSAEUS checkout" message every time.
# Living inside the repository is what makes the path knowable.
MUSAEUS_SRC = Path(__file__).resolve().parents[2] / "musaeus" / "stages" / "dupe_resolver.py"


# --------------------------------------------------------------------------
# Live detection, borrowed rather than reinvented
# --------------------------------------------------------------------------
def load_musaeus_live_markers(src: Path) -> tuple[str, ...]:
    """Read _LIVE_MARKERS out of MUSAEUS's source without importing the package.

    Importing musaeus.stages.dupe_resolver would drag in the whole package's
    dependency graph and a config load for the sake of one tuple of strings.

    That reasoning was RE-TESTED on 2026-09-15 when this tool moved into the
    repository, because "it is a plain import now" was the obvious thing to
    assume. It is not: the import costs 277 ms, pulls in 78 musaeus modules,
    and musaeus/config.py runs _load_env() at import time -- so merely
    importing it MUTATES os.environ. A read-only reporting tool has no
    business doing that, so the ast read stays.
    ast.literal_eval on the assignment gives the same value with no import side
    effects and no risk of this read-only tool initialising anything.

    Raises rather than defaulting. A silent fallback copy is how the same rule
    ends up with two different answers.
    """
    if not src.is_file():
        raise SystemExit(
            f"Cannot read MUSAEUS live-detection rule: {src} not found.\n"
            "This tool deliberately has no private copy of that rule. Point\n"
            "--musaeus-src at a MUSAEUS checkout."
        )
    tree = ast.parse(src.read_text(encoding="utf-8"), filename=str(src))
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        names = {t.id for t in targets if isinstance(t, ast.Name)}
        if "_LIVE_MARKERS" not in names or node.value is None:
            continue
        markers = ast.literal_eval(node.value)
        if not isinstance(markers, (tuple, list)) or not all(isinstance(m, str) for m in markers):
            raise SystemExit(f"_LIVE_MARKERS in {src} is not a tuple of strings.")
        return tuple(markers)
    raise SystemExit(
        f"_LIVE_MARKERS not found in {src}.\n"
        "It may have been renamed or moved. Fix this loader to follow it rather\n"
        "than copying the marker list here -- one rule, one home."
    )


def build_live_re(markers: tuple[str, ...]) -> re.Pattern[str]:
    """Rebuild MUSAEUS's _LIVE_RE exactly: longest-first alternation, \\b bounded."""
    return re.compile(
        r"\b(?:" + "|".join(re.escape(w) for w in sorted(markers, key=len, reverse=True)) + r")\b",
        re.IGNORECASE,
    )


#: Unambiguous live phrasing. Every one of these means a live recording; none of
#: them occurs in an ordinary studio song title. "live at"/"in"/"from" plus a
#: venue is the strongest signal there is.
CERTAIN_RE = re.compile(
    rf"""
      \blive \s* (?: at | in | from | on \s+ stage | version | recording | cut ) \b
    | \bconcert \s+ version\b                # The Band, "Rag Mama Rag (Concert Version)"
      # "live" or "concert" ANYWHERE inside a bracket, not just at the front.
      # Requiring it first missed "(Bonus Live Excerpt)" and "(Homecoming Live)",
      # both of which are live recordings, and dropped them into DOUBTFUL where
      # they would have been dismissed as false positives.
    | [{OPEN}] [^{CLOSE}]* \b(?: live | concert ) \b [^{CLOSE}]* [{CLOSE}]
    | \blive \s* [-–—] \s*                   # "Whipping Post - Live"
    | \bunplugged\b
    | \bin \s+ concert\b
    | \bat \s+ the \s+ bbc\b
    | \b(?: bbc | radio | live ) \s+ session s? \b
    | \blive \s* $                           # title ending in the word Live
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: "concert" on its own -- Concert for Bangladesh, concerto, "Concert in the Park".
#: Real but weaker than the above, and it is a MUSAEUS marker so it must be tiered
#: somewhere rather than dropped.
CONCERTISH_RE = re.compile(r"\bconcert\b", re.IGNORECASE)


@dataclass
class Track:
    id: int
    file_path: str
    artist: str
    album: str
    title: str
    year: str
    duration: float | None
    codec: str
    bitrate: int | None

    tier: str = ""          # CERTAIN | LIKELY | DOUBTFUL
    why: str = ""           # the phrase that matched, quoted back
    matched_in: str = ""    # title | album | both
    have_studio: bool = False
    studio_examples: list[str] = field(default_factory=list)

    @property
    def exists(self) -> bool:
        return bool(self.file_path) and os.path.isfile(self.file_path)


# --------------------------------------------------------------------------
# Matching a live track to its studio sibling
# --------------------------------------------------------------------------
# Bracket classes from musaeus.brackets, not a private copy. Each of
# these covered parens and squares but NOT braces -- the omission that
# rule exists to catch, and invisible while this lived outside the repo.
_PAREN_RE = re.compile(rf"[{OPEN}][^{CLOSE}]*[{CLOSE}]")
_DASH_TAIL_RE = re.compile(r"\s+[-–—]\s+.*$")
_PUNCT_RE = re.compile(r"[^\w\s]+")
_WS_RE = re.compile(r"\s+")

#: Stripped from a title before comparing versions. These describe the PRESSING,
#: not the song, so "Whipping Post (Live at Fillmore East)" and "Whipping Post"
#: have to reduce to the same key or the studio sibling is never found.
_VERSION_NOISE = (
    "live", "in concert", "concert", "unplugged", "at the bbc", "bbc session",
    "radio session", "live session", "remaster", "remastered", "remasterd",
    "mono", "stereo", "single version", "album version", "radio edit",
    "extended", "reissue", "deluxe", "bonus track", "digitally remastered",
    "original", "version", "edit", "take", "alternate", "demo",
)
_NOISE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in sorted(_VERSION_NOISE, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def norm_key(s: str) -> str:
    """Fold a string for comparison: accents, case, punctuation, spacing."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _PUNCT_RE.sub(" ", s.lower())
    return _WS_RE.sub(" ", s).strip()


def song_key(artist: str, title: str) -> tuple[str, str]:
    """(artist, song) with version qualifiers removed, for sibling matching.

    Deliberately strips parenthesised and trailing-dash segments BEFORE removing
    noise words, because that is where version information almost always lives.
    The risk is real and accepted: a song whose actual title is parenthetical
    loses part of itself. That produces a missed or spurious sibling flag, which
    is a wrong priority on one row -- not a wrong file operation, because this
    tool performs none.
    """
    t = _PAREN_RE.sub(" ", title or "")
    t = _DASH_TAIL_RE.sub(" ", t)
    t = _NOISE_RE.sub(" ", t)
    t = norm_key(t)
    a = norm_key(_PAREN_RE.sub(" ", artist or ""))
    for lead in ("the ",):
        if a.startswith(lead):
            a = a[len(lead):]
    return a, t


def classify(title: str, album: str, live_re: re.Pattern[str]) -> tuple[str, str, str]:
    """(tier, why, matched_in) for a row MUSAEUS's regex already flagged."""
    title, album = title or "", album or ""

    for field_name, text in (("title", title), ("album", album)):
        m = CERTAIN_RE.search(text)
        if m:
            other = album if field_name == "title" else title
            both = bool(CERTAIN_RE.search(other))
            return "CERTAIN", m.group(0).strip(), "both" if both else field_name

    # Nothing unambiguous. Album-level evidence still beats title-level here:
    # a clean song title on an album called "Live at Leeds" is a live recording,
    # whereas the bare word "live" inside a song title usually is not.
    if live_re.search(album):
        m = live_re.search(album) or CONCERTISH_RE.search(album)
        return "LIKELY", (m.group(0).strip() if m else "album marker"), "album"

    m = CONCERTISH_RE.search(title)
    if m:
        return "LIKELY", m.group(0).strip(), "title"

    m = live_re.search(title)
    return "DOUBTFUL", (m.group(0).strip() if m else "live"), "title"


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
def fetch(db: Path, status: str) -> list[sqlite3.Row]:
    if not db.is_file():
        raise SystemExit(f"Vault database not found: {db}")
    uri = f"file:{db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            SELECT id, file_path, COALESCE(artist,'') AS artist,
                   COALESCE(album,'')  AS album,  COALESCE(title,'') AS title,
                   COALESCE(year,'')   AS year,   duration,
                   COALESCE(codec,'')  AS codec,  bitrate
              FROM archive
             WHERE status = ?
             ORDER BY artist, album, track, title
            """,
            (status,),
        ).fetchall()
    finally:
        conn.close()


def analyse(rows: list[sqlite3.Row], live_re: re.Pattern[str]) -> tuple[list[Track], int]:
    live: list[Track] = []
    studio_index: dict[tuple[str, str], list[sqlite3.Row]] = {}

    for r in rows:
        if live_re.search(f"{r['title']} {r['album']}"):
            continue
        # A non-live row is a studio-sibling candidate. Indexed by folded song
        # key so the lookup below is exact rather than a scan.
        studio_index.setdefault(song_key(r["artist"], r["title"]), []).append(r)

    for r in rows:
        if not live_re.search(f"{r['title']} {r['album']}"):
            continue
        tier, why, where = classify(r["title"], r["album"], live_re)
        t = Track(
            id=r["id"], file_path=r["file_path"], artist=r["artist"], album=r["album"],
            title=r["title"], year=r["year"], duration=r["duration"],
            codec=r["codec"], bitrate=r["bitrate"], tier=tier, why=why, matched_in=where,
        )
        siblings = studio_index.get(song_key(r["artist"], r["title"]), [])
        t.have_studio = bool(siblings)
        t.studio_examples = [
            f"{s['title']}" + (f" [{s['album']}]" if s["album"] else "") for s in siblings[:3]
        ]
        live.append(t)

    return live, len(studio_index)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def write_m3u(tracks: list[Track], path: Path, title: str) -> int:
    """Extended M3U. Absolute paths, so it plays from anywhere.

    Only tracks whose file is actually on disk are written -- a playlist with
    dead entries is one most players silently skip, which would hide rows from
    a list whose entire purpose is to be worked through.
    """
    written = 0
    with path.open("w", encoding="utf-8") as fh:
        fh.write("#EXTM3U\n")
        fh.write(f"#PLAYLIST:{title}\n")
        for t in tracks:
            if not t.exists:
                continue
            secs = int(round(t.duration)) if t.duration else -1
            label = f"{t.artist} - {t.title}" if t.artist else t.title
            fh.write(f"#EXTINF:{secs},{label}\n")
            fh.write(f"{t.file_path}\n")
            written += 1
    return written


CSV_COLUMNS = [
    "priority", "confidence", "have_studio", "artist", "title", "album", "year",
    "duration_mmss", "matched_on", "matched_text", "studio_copy_you_already_own",
    "codec", "bitrate", "file_exists", "file_path",
]


def mmss(d: float | None) -> str:
    if not d:
        return ""
    d = int(round(d))
    return f"{d // 60}:{d % 60:02d}"


def write_csv(tracks: list[Track], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for t in tracks:
            # HUNT is the live-only work; HAVE-BOTH needs nothing; CHECK is the
            # doubtful tier. Sorting on this one column gives a worklist.
            if t.tier == "DOUBTFUL":
                priority = "3-CHECK"
            elif t.have_studio:
                priority = "2-HAVE-BOTH"
            else:
                priority = "1-HUNT"
            w.writerow({
                "priority": priority,
                "confidence": t.tier,
                "have_studio": "yes" if t.have_studio else "no",
                "artist": t.artist, "title": t.title, "album": t.album, "year": t.year,
                "duration_mmss": mmss(t.duration),
                "matched_on": t.matched_in, "matched_text": t.why,
                "studio_copy_you_already_own": " | ".join(t.studio_examples),
                "codec": t.codec, "bitrate": t.bitrate or "",
                "file_exists": "yes" if t.exists else "MISSING",
                "file_path": t.file_path,
            })


def summarise(tracks: list[Track], studio_songs: int, out: list[str]) -> None:
    """Short lines, grouped, counts first. Not a wall of paths."""
    def n(tier=None, have=None):
        return sum(
            1 for t in tracks
            if (tier is None or t.tier == tier) and (have is None or t.have_studio == have)
        )

    real = [t for t in tracks if t.tier in ("CERTAIN", "LIKELY")]
    doubtful = [t for t in tracks if t.tier == "DOUBTFUL"]
    hunt = [t for t in real if not t.have_studio]
    have = [t for t in real if t.have_studio]

    out.append(f"Live recordings found      {len(real)}")
    out.append(f"  CERTAIN                  {n('CERTAIN')}")
    out.append(f"  LIKELY  (album says so)  {n('LIKELY')}")
    out.append("")
    # These two must sum to len(real). An earlier version counted "have studio"
    # across all tiers while counting the hunting list across two, so the summary
    # printed 62 + 49 against a total of 110. A summary whose arithmetic does not
    # close is worse than no summary.
    out.append(f"THE HUNTING LIST           {len(hunt)}  (live is the only copy you own)")
    out.append(f"Already have studio too    {len(have)}  (nothing to do)")
    assert len(hunt) + len(have) == len(real), "summary counts must sum to the total"
    out.append("")
    out.append(f"Set aside as DOUBTFUL      {len(doubtful)}  (the word 'live' in an otherwise studio title)")
    for t in doubtful:
        out.append(f"     {t.artist} - {t.title}")
    out.append("")
    out.append(f"Studio songs indexed       {studio_songs}")

    by_artist: dict[str, int] = {}
    for t in hunt:
        by_artist[t.artist or "(no artist)"] = by_artist.get(t.artist or "(no artist)", 0) + 1
    if by_artist:
        out.append("")
        out.append("Most to hunt, by artist:")
        for a, c in sorted(by_artist.items(), key=lambda kv: (-kv[1], kv[0]))[:12]:
            out.append(f"  {c:>3}  {a}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Live-version hunting list. Read-only.")
    p.add_argument("--db", type=Path, default=VAULT_DB)
    p.add_argument("--musaeus-src", type=Path, default=MUSAEUS_SRC)
    p.add_argument("--status", default="CATALOGUED")
    p.add_argument("--out-dir", type=Path, default=Path.home() / "Desktop")
    args = p.parse_args(argv)

    markers = load_musaeus_live_markers(args.musaeus_src)
    live_re = build_live_re(markers)

    rows = fetch(args.db, args.status)
    tracks, studio_songs = analyse(rows, live_re)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    order = {"CERTAIN": 0, "LIKELY": 1, "DOUBTFUL": 2}
    tracks.sort(key=lambda t: (t.have_studio, order.get(t.tier, 9), t.artist.lower(), t.title.lower()))

    csv_path = args.out_dir / "LIVE_TRACKS_hunting_list.csv"
    write_csv(tracks, csv_path)

    hunt = [t for t in tracks if t.tier in ("CERTAIN", "LIKELY") and not t.have_studio]
    both = [t for t in tracks if t.tier in ("CERTAIN", "LIKELY") and t.have_studio]
    m3u_hunt = args.out_dir / "LIVE_hunt_studio_wanted.m3u"
    m3u_both = args.out_dir / "LIVE_have_studio_too.m3u"
    n_hunt = write_m3u(hunt, m3u_hunt, "Live - studio version wanted")
    n_both = write_m3u(both, m3u_both, "Live - studio already owned")

    report: list[str] = [f"Rows read: {len(rows)} with status {args.status}", ""]
    summarise(tracks, studio_songs, report)
    report += [
        "",
        f"Live markers reused from: {args.musaeus_src}",
        f"  {', '.join(markers)}",
        "",
        "Written:",
        f"  {csv_path}",
        f"  {m3u_hunt}   ({n_hunt} playable)",
        f"  {m3u_both}   ({n_both} playable)",
    ]
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
