#!/usr/bin/env python3
"""Propose album names for catalogued tracks that have none.

READ-ONLY on the vault. Opens the database `mode=ro`, writes ONE CSV and its
own answer cache, and touches no audio file, no tag and no database row. It
proposes; you rule. `apply_album_names.py` beside this file is the half that
writes, and it is deliberately a separate program.

Why this is in the repository
-----------------------------
It lived in ~/Desktop/POST.Code/ until 2026-09-14, and being outside the
repository cost it the one rule that mattered most.

MUSAEUS stores the article as a suffix -- "Beatles, The" -- so a
folder-browsed library sorts under B. No music service has ever heard of
that string. `musaeus/artist_form.py` exists precisely to say so, and had
already measured it on 2026-08-29: *376 of 839 cached misses were in `X, The`
form, and 0 of 2,158 hits were.* Not one article-suffix lookup had ever
succeeded.

A standalone script cannot import that module, so this one queried the
stored form and got silence. Measured 2026-09-14 on the 6,142-row proposal
CSV: **1,072 rows carried a trailing-article artist and 1,071 of them --
99.9% -- came back "4-NO ANSWER"**, against 0% of the rows where the sources
merely disagreed. The Beatles, The Who, The Chieftains, The Rolling Stones:
none of them were ever actually asked about.

Spot-check of the same query in both forms:

    "Who, The"  + "Baba O'Riley"  ->  itunes ''                deezer ''
    "The Who"   + "Baba O'Riley"  ->  itunes "Who's Next (DE)" deezer "Who's Next (DE)"

That second line is a 1-AGREED that was being thrown away. Every lookup now
goes through natural_form(), which is article-aware and respects
PROTECTED_ARTIST_NAMES, so "De La Soul" and "Los Lobos" survive unchanged.

Sources
-------
iTunes, Deezer and MusicBrainz, by agreement rather than by trust: two
sources naming the same album is the signal, because any one of them will
confidently return a compilation.

MusicBrainz replaced Spotify on 2026-09-14. Spotify issues a token happily
and then answers /v1/search with HTTP 403 "Active premium subscription
required for the owner of the app" -- a token is not permission to search,
and no code change fixes an account requirement. MusicBrainz needs no key at
all, and ORPHEUS used it for this same job. Its own relevance score is NOT
trusted: a search for Fleetwood Mac's "Dreams" returns "Mac dreams" at score
100. Matching is by the same strict artist/title folding used for the other
two.
"""

from __future__ import annotations

import argparse

import contextlib
import csv
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

VERSION = "3.0"

# Importable when run as `python3 scripts/album_names/propose_album_names.py`
# from a checkout, which is how the console and the docs invoke it.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from musaeus.artist_form import natural_form  # noqa: E402

VAULT_DB = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/musaeus.db")
DEFAULT_CACHE = Path.home() / ".cache" / "album-names" / "cache.db"
DEFAULT_OUT = Path.home() / "Desktop" / "MUSAEUS_album_names_PROPOSED.csv"

# MusicBrainz requires a contactable User-Agent and throttles anonymous
# clients to ~1 request/second. Identify honestly rather than hide.
_FALLBACK_UA = "MUSAEUS-album-names/1.0 (https://github.com/BogMan64/musaeus)"

#: MusicBrainz asks for no more than one request a second and means it.
MB_MIN_GAP_S = 1.1


class FetchError(RuntimeError):
    """A source could not be reached."""


def _get_json(url: str, headers: dict | None = None) -> dict | None:
    req_headers = {"User-Agent": _FALLBACK_UA}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc


RATE_S = 1.5

COMPILATION_RE = re.compile(
    r"\b(greatest\s+hits|best\s+of|the\s+best|anthology|collection|essential|"
    r"very\s+best|ultimate|definitive|retrospective|hits|gold|singles|"
    r"compilation|volume\s+\d|vol\.?\s*\d|now\s+that'?s|super\s+hits)\b",
    re.I,
)

SINGLE_RE = re.compile(r"\s+-\s+(single|ep)\s*$", re.I)

_TITLE_NOISE = re.compile(
    r"\s*[\(\[](?:feat\.?|ft\.?|featuring|with)\s[^)\]]*[\)\]]"
    r"|\s*[\(\[][^)\]]*(?:remaster|remastered|version|edit|mix|mono|stereo|"
    r"single|album|radio|explicit|clean|bonus|deluxe|expanded)[^)\]]*[\)\]]"
    r"|\s+-\s+(?:single|ep)\s*$",
    re.I,
)


def fold(s: str, *, strip_noise: bool = False) -> str:
    s = s or ""
    if strip_noise:
        s = _TITLE_NOISE.sub("", s)
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s)


def _year_of(release_date: str) -> int:
    m = re.match(r"(\d{4})", release_date or "")
    return int(m.group(1)) if m else 9999


SINGLE_SUFFIX_SLACK = 6


def _is_single_release(track: str, collection: str) -> bool:
    c = fold(collection, strip_noise=True)
    t = fold(track, strip_noise=True)
    if not c:
        return False
    return c == t or (c.startswith(t) and len(c) - len(t) < SINGLE_SUFFIX_SLACK)


@dataclass
class Answer:
    album: str = ""
    year: str = ""
    source: str = ""
    is_self_titled_fallback: bool = False

    @property
    def is_compilation(self) -> bool:
        return bool(self.album and COMPILATION_RE.search(self.album))


class Throttle:
    """Per-source pacing, with a floor a source can insist on.

    `--rate` is the operator's choice for how hard to push, but MusicBrainz
    enforces ~1 request/second for anonymous clients and answers 503 when
    that is ignored. A shared gap would either throttle iTunes and Deezer
    needlessly or get MusicBrainz blocked, so each source keeps its own gap
    and a source-specific minimum wins when it is slower.
    """

    #: Gaps a source requires regardless of --rate.
    MINIMUMS = {"musicbrainz": MB_MIN_GAP_S}

    def __init__(self, gap: float = RATE_S):
        self.gap = gap
        self._last: dict[str, float] = {}

    def gap_for(self, key: str) -> float:
        return max(self.gap, self.MINIMUMS.get(key, 0.0))

    def wait(self, key: str) -> None:
        gap = self.gap_for(key)
        last = self._last.get(key, 0.0)
        delta = time.monotonic() - last
        if delta < gap:
            time.sleep(gap - delta)
        self._last[key] = time.monotonic()


def _choose(candidates: list[tuple[str, str, str, str]]) -> Answer:
    if not candidates:
        return Answer()

    distinct_albums = [c for c in candidates if not _is_single_release(c[0], c[1])]

    if distinct_albums:
        pool = distinct_albums
        is_fallback = False
    else:
        pool = candidates
        is_fallback = True

    pool.sort(
        key=lambda c: (
            _year_of(c[2]),
            bool(COMPILATION_RE.search(c[1])),
            len(c[1]),
        )
    )
    _track, collection, year, source = pool[0]
    cleaned = SINGLE_RE.sub("", collection).strip()
    return Answer(
        album=cleaned,
        year=(year or "")[:4],
        source=source,
        is_self_titled_fallback=is_fallback,
    )


def ask_musicbrainz(artist: str, title: str, throttle: Throttle, **_kw) -> Answer:
    """Third source, free and keyless.

    MusicBrainz's own ext:score is deliberately ignored. A query for
    Fleetwood Mac's "Dreams" returns a recording titled "Mac dreams" at
    score 100, so the score measures string proximity, not correctness.
    Candidates are filtered by the same strict artist/title folding as
    iTunes and Deezer, then handed to _choose, which already prefers the
    earliest non-compilation release.
    """
    throttle.wait("musicbrainz")
    query = f'artist:"{artist}" AND recording:"{title}"'
    url = (
        "https://musicbrainz.org/ws/2/recording?query="
        + urllib.parse.quote(query)
        + "&fmt=json&limit=15"
    )
    data = _get_json(url) or {}
    want_title = fold(title, strip_noise=True)
    want_artist = fold(artist)

    candidates = []
    for rec in data.get("recordings") or []:
        if fold(rec.get("title", ""), strip_noise=True) != want_title:
            continue
        credits = rec.get("artist-credit") or []
        if not any(fold((c.get("artist") or {}).get("name", "")) == want_artist for c in credits):
            continue
        for rel in rec.get("releases") or []:
            album = rel.get("title", "") or ""
            if album:
                candidates.append(
                    (rec.get("title", ""), album, rel.get("date", "") or "", "musicbrainz")
                )
    return _choose(candidates)


def ask_itunes(artist: str, title: str, throttle: Throttle, **_kw) -> Answer:
    throttle.wait("itunes")
    term = urllib.parse.quote(f"{artist} {title}")
    url = f"https://itunes.apple.com/search?term={term}&media=music&entity=song&limit=15"
    data = _get_json(url) or {}
    want_title = fold(title, strip_noise=True)
    want_artist = fold(artist)

    candidates = []
    for r in data.get("results") or []:
        collection = r.get("collectionName", "") or ""
        if not collection:
            continue
        if fold(r.get("trackName", ""), strip_noise=True) != want_title:
            continue
        if fold(r.get("artistName", "")) != want_artist:
            continue
        candidates.append(
            (r.get("trackName", ""), collection, r.get("releaseDate", ""), "itunes")
        )
    return _choose(candidates)


def ask_deezer(artist: str, title: str, throttle: Throttle, **_kw) -> Answer:
    throttle.wait("deezer")
    q = urllib.parse.quote(f"{artist} {title}")
    url = f"https://api.deezer.com/search?q={q}&limit=15"
    data = _get_json(url) or {}
    want_title = fold(title, strip_noise=True)
    want_artist = fold(artist)

    candidates = []
    for r in data.get("data") or []:
        album = ((r.get("album") or {}).get("title", "")) or ""
        if not album:
            continue
        if fold(r.get("title", ""), strip_noise=True) != want_title:
            continue
        if fold((r.get("artist") or {}).get("name", "")) != want_artist:
            continue
        candidates.append((r.get("title", ""), album, "", "deezer"))
    return _choose(candidates)


def verdict(itunes: Answer, deezer: Answer, third: Answer | None = None) -> tuple[str, str, str, Answer]:
    """Agreement between any two sources is the signal.

    `third` was Spotify and is now MusicBrainz; the tier names never
    mentioned it, so nothing downstream changes.
    """
    third = third or Answer()
    valid = [a for a in (itunes, deezer, third) if a.album]
    if not valid:
        return ("4-NO ANSWER", "", "", Answer())

    groups: dict[str, list[Answer]] = {}
    for a in valid:
        groups.setdefault(fold(a.album), []).append(a)

    sorted_groups = sorted(groups.values(), key=len, reverse=True)
    top_group = sorted_groups[0]

    if len(top_group) > 1:
        winner = sorted(
            top_group,
            key=lambda a: (bool(a.year), a.source == "itunes", a.source == "musicbrainz"),
            reverse=True,
        )[0]
        sources = ", ".join(sorted(set(a.source for a in top_group)))
        return ("1-AGREED", winner.album, f"Agreed by: {sources}", winner)

    if len(valid) == 1:
        winner = valid[0]
        return (f"2-{winner.source.upper()} ONLY", winner.album, "", winner)

    winner = itunes if itunes.album else (third if third.album else deezer)
    note = f"Disagreement. Selected {winner.source} proposal ({winner.album})."
    return ("3-SOURCES DISAGREE", winner.album, note, winner)


CACHE_DDL = """
CREATE TABLE IF NOT EXISTS answer (
    artist TEXT, title TEXT, source TEXT,
    album TEXT, year TEXT, is_self_titled INTEGER DEFAULT 0,
    asked_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (artist, title, source)
)"""


def cache_open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(CACHE_DDL)
    cols = [r[1] for r in con.execute("PRAGMA table_info(answer)").fetchall()]
    if "is_self_titled" not in cols:
        con.execute("ALTER TABLE answer ADD COLUMN is_self_titled INTEGER DEFAULT 0")
    con.commit()
    return con


def cache_get(con: sqlite3.Connection, artist: str, title: str, source: str) -> Answer | None:
    row = con.execute(
        "SELECT album, year, source, is_self_titled FROM answer WHERE artist=? AND title=? AND source=?",
        (artist, title, source),
    ).fetchone()
    return Answer(row[0], row[1], row[2], bool(row[3])) if row else None


def cache_put(con: sqlite3.Connection, artist: str, title: str, source: str, answer: Answer) -> None:
    con.execute(
        "INSERT OR REPLACE INTO answer(artist,title,source,album,year,is_self_titled) VALUES (?,?,?,?,?,?)",
        (artist, title, source, answer.album, answer.year, int(answer.is_self_titled_fallback)),
    )
    con.commit()


#: Where the console's API-key menu writes what it collects.
MUSAEUS_CONFIG_DIR = Path.home() / ".config" / "musaeus"


def _load_musaeus_credentials(config_dir: Path | None = None) -> list[str]:
    """Load MUSAEUS's own env files into os.environ, without overriding.

    setdefault, not assignment: a shell export is a deliberate override for
    one run and must keep winning, which is the same precedence
    musaeus/config.py uses for these files.

    Returns the key names newly set, so the caller can say what it picked up.
    """
    loaded: list[str] = []
    base = config_dir or MUSAEUS_CONFIG_DIR
    for name in ("settings.env", "credentials.env"):
        path = base / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip("\"'")
            if key and val and key not in os.environ:
                os.environ[key] = val
                loaded.append(key)
    return loaded


def load_targets(
    db: Path, limit: int, path_prefix: str | None = None
) -> list[tuple[int, str, str, str]]:
    if not db.is_file():
        raise SystemExit(f"Vault database not found: {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    sql = """
            SELECT id, COALESCE(artist,''), COALESCE(title,''), COALESCE(file_path,'')
              FROM archive
             WHERE status = 'CATALOGUED'
               AND (COALESCE(album,'') = ''
                    OR album LIKE 'My playlist%'
                    OR album LIKE '%Unknown%')
               AND COALESCE(artist,'') <> ''
               AND COALESCE(title,'')  <> ''
    """
    params: list[str] = []
    if path_prefix:
        # Bound, never interpolated -- this string comes off the command line.
        # LIKE with an anchored prefix so "…/Libraries/ALAC-Archival" cannot
        # also match a path that merely contains it somewhere in the middle.
        sql += " AND file_path LIKE ? ESCAPE '\\'"
        escaped = path_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(escaped + "%")
    sql += " ORDER BY artist, title"
    try:
        rows = con.execute(sql, params).fetchall()
        return [tuple(r) for r in rows][:limit] if limit else [tuple(r) for r in rows]
    finally:
        con.close()


COLUMNS = [
    "confidence", "artist", "title", "proposed_album", "year", "is_self_titled",
    "itunes_says", "deezer_says", "musicbrainz_says", "sources_agree", "looks_like_compilation",
    "note", "archive_id", "file_path",
]

CONFIDENCE_ORDER = {
    "1-AGREED": 0,
    "2-ITUNES ONLY": 1,
    "2-DEEZER ONLY": 1,
    "2-MUSICBRAINZ ONLY": 1,
    "3-SOURCES DISAGREE": 2,
    "4-NO ANSWER": 3,
}


def write_csv(rows: list[dict], out: Path) -> None:
    rows = sorted(
        rows,
        key=lambda r: (
            CONFIDENCE_ORDER.get(r["confidence"], 9),
            r["looks_like_compilation"] == "yes",
            r["artist"].lower(),
            r["title"].lower(),
        ),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=VAULT_DB)
    ap.add_argument("--limit", type=int, default=0, help="max tracks to consider (0 = all)")
    ap.add_argument("--rate", type=float, default=RATE_S, help="seconds between requests, per source")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--dry-run", action="store_true", help="counts only, no network, no cache read")
    ap.add_argument("--offline", action="store_true", help="use only cached answers, make no request")
    ap.add_argument(
        "--path-prefix",
        default=None,
        help="only consider rows whose file_path starts with this "
             "(e.g. /mnt/.../Libraries/ALAC-Archival)",
    )
    args = ap.parse_args(argv)

    # Keys entered through the console's "Enter/Update API Keys" menu land in
    # ~/.config/musaeus/credentials.env, NOT in the environment. Reading only
    # os.environ meant this script silently ran two-source while Spotify
    # credentials sat configured and unused -- no error, just a quieter answer
    # and a "2 sources" line nobody had reason to question.
    _load_musaeus_credentials()

    # Three sources, none of them trusted alone. MusicBrainz gets its own
    # slower throttle: it asks for <=1 req/s from anonymous clients and
    # enforces it, while iTunes and Deezer tolerate the default rate.
    active_sources = [
        ("itunes", ask_itunes),
        ("deezer", ask_deezer),
        ("musicbrainz", ask_musicbrainz),
    ]

    targets = load_targets(args.db, args.limit, args.path_prefix)
    if args.path_prefix:
        print(f"restricted to             : {args.path_prefix}")
    print(f"tracks with no album name : {len(targets):,}")
    print(f"sources active            : {', '.join(n for n, _ in active_sources)}")

    if args.dry_run:
        num_src = len(active_sources)
        print(f"DRY RUN -- no network, no cache touched. {len(targets):,} track(s) would be asked about.")
        print(f"estimated requests: {len(targets) * num_src:,} across {num_src} sources")
        print(f"estimated time: ~{len(targets) * num_src * args.rate / 3600:.1f} hour(s) at {args.rate}s/request")
        return 0

    con = cache_open(args.cache)
    cached = con.execute("SELECT COUNT(*) FROM answer").fetchone()[0]
    print(f"answers already cached     : {cached:,}")
    if not args.offline:
        todo = sum(
            1
            for _id, artist, title, _fp in targets
            for source, _ in active_sources
            if cache_get(con, natural_form(artist), title, source) is None
        )
        print(f"requests still to make     : {todo:,}  (~{todo * args.rate / 3600:.1f} h at {args.rate}s each)")
    print()

    throttle = Throttle(args.rate)
    rows: list[dict] = []
    interrupted = False

    try:
        for n, (archive_id, artist, title, file_path) in enumerate(targets, 1):
            # THE fix this file exists for. The library stores "Beatles, The"
            # so folders sort under B; no service has heard of that string.
            # Ask in the natural form, and cache under it too -- the two
            # spellings are one artist, so they must share one cache row or
            # the same lookup gets made twice and answered once.
            lookup_artist = natural_form(artist)

            answers: dict[str, Answer] = {}
            for source, fn in active_sources:
                hit = cache_get(con, lookup_artist, title, source)
                if hit is None and not args.offline:
                    try:
                        hit = fn(lookup_artist, title, throttle)
                    except FetchError as exc:
                        # A transport failure is NOT an answer. Caching it
                        # would record "this source has nothing for this
                        # track" permanently, so a later healthy run would
                        # read the poisoned row and never ask again -- one
                        # rate-limit blip turning into a permanent blank.
                        print(f"  ! {source} failed for {artist} - {title}: {exc}")
                        answers[source] = Answer()
                        continue
                    cache_put(con, lookup_artist, title, source, hit)
                answers[source] = hit or Answer()

            itunes_ans = answers.get("itunes", Answer())
            deezer_ans = answers.get("deezer", Answer())
            mb_ans = answers.get("musicbrainz", Answer())

            confidence, album, note, winning_ans = verdict(itunes_ans, deezer_ans, mb_ans)
            is_comp = Answer(album=album).is_compilation

            rows.append(
                {
                    "confidence": confidence,
                    "artist": artist,
                    "title": title,
                    "proposed_album": album,
                    "year": winning_ans.year or itunes_ans.year or mb_ans.year or "",
                    "is_self_titled": "yes" if winning_ans.is_self_titled_fallback else "no" if album else "",
                    "itunes_says": itunes_ans.album,
                    "deezer_says": deezer_ans.album,
                    "musicbrainz_says": mb_ans.album,
                    "sources_agree": (
                        "yes" if confidence == "1-AGREED"
                        else "" if confidence == "4-NO ANSWER"
                        else "no"
                    ),
                    "looks_like_compilation": "yes" if is_comp else "",
                    "note": note,
                    "archive_id": archive_id,
                    "file_path": file_path,
                }
            )
            if n % 100 == 0 or n == len(targets):
                print(f"  {n:,}/{len(targets):,} processed")
    except KeyboardInterrupt:
        interrupted = True
        print(f"\ninterrupted at {len(rows):,}/{len(targets):,} -- writing what's done so far")

    write_csv(rows, args.out)

    counts = Counter(r["confidence"] for r in rows)
    print()
    for key in sorted(counts, key=lambda k: CONFIDENCE_ORDER.get(k, 9)):
        print(f"  {key:<20} {counts[key]:>6}")
    comps = sum(1 for r in rows if r["looks_like_compilation"] == "yes")
    st_count = sum(1 for r in rows if r["is_self_titled"] == "yes")
    print(f"  {'flagged compilation':<20} {comps:>6}")
    print(f"  {'self-titled fallback':<20} {st_count:>6}")
    print()
    print(f"-> {args.out}")
    print("Nothing was written to the database or to any audio file.")
    return 1 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
