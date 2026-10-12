"""Genre suggestions from MusicBrainz and Wikipedia (Grey, 2026-10-11).

Grey's genre review on 2026-10-11 was done by hand with chat2: a sheet of
artists, a look at what MusicBrainz and Wikipedia call each one, and a
decision per artist. Grey asked for MUSAEUS to do the looking:

    musaeus genre-suggest generate     look each act up, write a sheet
    musaeus genre-suggest apply SHEET  load the rows Grey approved

What it asks:

  MusicBrainz   the artist's genre votes (artist/<mbid>?inc=genres). The
                act's MBID comes from the catalogue (mb_artist_id), or a name
                search with mb_enrich's own guard ("Dion" is not Celine Dion).
  Wikipedia     the "genre" field of the article's infobox. The article is
                found through MusicBrainz's own link to it (a Wikipedia or
                Wikidata relation), never by guessing a title.

Every answer is mapped to Genre_Allowed.txt -- an exact match or a
Genre_Canonical_Map.txt rule, never a fuzzy guess -- and what does not map
is listed on the sheet, so the map can learn it. Every request goes through
the network gateway.

It never changes a genre by itself. `generate` reads the catalogue (read-only)
and writes a sheet. `apply` changes MasterLaw.csv only for the rows Grey
marked, after a backup, under the masters lock; the next GenreValidate (Act 1)
then brings the library to it.
"""

from __future__ import annotations

import contextlib
import csv
import datetime
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import urllib.error
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .network_policy import check as _network_check

logger = logging.getLogger(__name__)

_WIKI_API = "https://en.wikipedia.org/w/api.php"
_WIKIDATA = "https://www.wikidata.org/wiki/Special:EntityData"
_USER_AGENT = "Musaeus/1.0 (personal music library; genre suggestions)"
_TIMEOUT_S = 15
_MB_PAUSE_S = 1.1  # MusicBrainz: at most one request a second
_WIKI_PAUSE_S = 0.2

#: The sheet's columns, in order. Grey marks `approve` (yes) and may type a
#: different genre over `suggested_genre`; nothing else is read back.
SHEET_FIELDS = [
    "approve",
    "artist",
    "songs",
    "current_genre",
    "suggested_genre",
    "agreement",
    "musicbrainz",
    "wikipedia",
    "not_in_vocabulary",
    "notes",
]

_APPROVED = {"yes", "y", "x"}


class LookupUnavailable(Exception):
    """No answer was obtained (timeout, 5xx, refused by the gateway): the act
    is not cached, so the next run asks again."""


# ── What the vocabulary understands ──────────────────────────────────────────


class Vocabulary:
    """Genre_Allowed.txt plus Genre_Canonical_Map.txt -- exact or mapped only.

    GenreCanon.resolve() falls back to a fuzzy match, which is fine for a
    tag that is already nearly a genre and wrong for a suggestion: "art pop"
    and "pop" are 80% alike and not the same advice. So this asks the same
    two files without the fuzzy step.
    """

    def __init__(self, allowed_path: Path, map_path: Path) -> None:
        from .canon.genre import GenreCanon

        canon = GenreCanon(allowed_path, map_path)
        self.allowed: set[str] = set(canon.all_allowed())
        self._lower = {g.lower(): g for g in self.allowed}
        self._map = dict(canon._map)  # lower raw -> canonical

    def resolve(self, raw: str) -> str | None:
        r = (raw or "").strip().lower()
        if not r:
            return None
        for cand in (r, r.removesuffix(" music"), r.replace("-", " "), r.replace(" ", "-")):
            if cand in self._lower:
                return self._lower[cand]
            mapped = self._map.get(cand)
            if mapped:
                return self._lower.get(mapped.lower(), mapped)
        return None

    def canonical(self, typed: str) -> str | None:
        """A genre Grey typed, as Genre_Allowed.txt spells it, or None."""
        return self._lower.get((typed or "").strip().lower())


# ── Reading a Wikipedia infobox ──────────────────────────────────────────────

_REF = re.compile(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", re.S | re.I)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_LINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_GENRE_FIELD = re.compile(r"^\s*\|\s*genres?\s*=(.*?)(?=^\s*\||^\s*\}\})", re.S | re.M | re.I)
_TEMPLATE_NAMES = {"hlist", "flatlist", "plainlist", "ubl", "unbulleted list", "flat list"}


def infobox_genres(wikitext: str) -> list[str]:
    """The genres in an infobox's "genre" field, in the order given.

    Handles links ([[Rock music|Rock]] -> "Rock"), {{hlist|..}} and
    {{flatlist| * ..}} lists, references and comments.
    """
    m = _GENRE_FIELD.search(wikitext or "")
    if not m:
        return []
    value = _COMMENT.sub("", _REF.sub("", m.group(1)))
    value = _LINK.sub(lambda x: x.group(2) or x.group(1), value)
    value = value.replace("{{", "|").replace("}}", "|")
    out: list[str] = []
    for part in re.split(r"[|\n*,•·]|<br\s*/?>", value):
        g = re.sub(r"\s+", " ", part).strip(" '\"")
        if not g or g.lower() in _TEMPLATE_NAMES or "=" in g:
            continue
        if g not in out:
            out.append(g)
    return out


# ── The lookups (each one through the gateway) ───────────────────────────────


def _get_json(url: str, gate: str) -> dict:
    req = Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        # Inside the try: a gateway refusal is "no answer" like a timeout --
        # the act is asked again next time, never recorded as looked up.
        _network_check(gate)
        with urlopen(req, timeout=_TIMEOUT_S) as resp:
            data: dict = json.loads(resp.read().decode("utf-8"))
            return data
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        raise LookupUnavailable(f"{gate}: HTTP {exc.code}") from exc
    except Exception as exc:  # timeout, DNS, a gateway refusal
        raise LookupUnavailable(f"{gate}: {exc}") from exc


@dataclass
class Found:
    """What the sources said about one act."""

    mbid: str | None = None
    musicbrainz: list[tuple[str, int]] = field(default_factory=list)  # (genre, votes)
    wikipedia: list[str] = field(default_factory=list)
    note: str = ""


def look_up(artist: str, mbid: str | None) -> Found:
    """Ask MusicBrainz, then the Wikipedia article it links to."""
    from .stages import mb_enrich

    found = Found(mbid=mbid)
    if not found.mbid:
        time.sleep(_MB_PAUSE_S)
        try:
            hit = mb_enrich._search_artist(artist)
        except mb_enrich.LookupUnavailable as exc:
            raise LookupUnavailable(str(exc)) from exc
        if hit is None:
            found.note = "MusicBrainz has no act by this name"
            return found
        found.mbid = hit[0]
    time.sleep(_MB_PAUSE_S)
    data = _get_json(
        f"{mb_enrich._MB_BASE}/artist/{found.mbid}?"
        + urlencode({"inc": "genres+url-rels", "fmt": "json"}),
        mb_enrich._MB_BASE,
    )
    found.musicbrainz = sorted(
        ((g["name"], int(g.get("count") or 0)) for g in data.get("genres", []) if g.get("name")),
        key=lambda gc: -gc[1],
    )
    title = _wikipedia_title(data.get("relations", []))
    if title:
        time.sleep(_WIKI_PAUSE_S)
        page = _get_json(
            _WIKI_API
            + "?"
            + urlencode(
                {
                    "action": "parse",
                    "page": title,
                    "prop": "wikitext",
                    "section": "0",
                    "format": "json",
                    "formatversion": "2",
                    "redirects": "1",
                }  # fmt: skip
            ),
            _WIKI_API,
        )
        found.wikipedia = infobox_genres((page.get("parse") or {}).get("wikitext", ""))
    else:
        found.note = "no Wikipedia article linked from MusicBrainz"
    return found


def _wikipedia_title(relations: Iterable[dict]) -> str | None:
    rels = list(relations)
    for r in rels:
        url = ((r.get("url") or {}).get("resource")) or ""
        m = re.match(r"https?://en\.wikipedia\.org/wiki/(.+)$", url)
        if r.get("type") == "wikipedia" and m:
            from urllib.parse import unquote

            return unquote(m.group(1)).replace("_", " ")
    for r in rels:
        url = ((r.get("url") or {}).get("resource")) or ""
        m = re.search(r"wikidata\.org/wiki/(Q\d+)", url)
        if r.get("type") == "wikidata" and m:
            time.sleep(_WIKI_PAUSE_S)
            q = m.group(1)
            data = _get_json(f"{_WIKIDATA}/{quote(q)}.json", _WIKIDATA)
            site = ((data.get("entities") or {}).get(q) or {}).get("sitelinks", {})
            title = (site.get("enwiki") or {}).get("title")
            if title:
                return str(title)
    return None


# ── One suggestion per act ───────────────────────────────────────────────────


@dataclass
class Suggestion:
    genre: str | None
    agreement: str
    unmapped: list[str]


def suggest(found: Found, vocab: Vocabulary) -> Suggestion:
    """MusicBrainz's most-voted genre and Wikipedia's first, as Grey's genres.

    When they agree, that is the suggestion. When they differ, MusicBrainz's
    (counted votes) is suggested and Wikipedia's shown beside it -- Grey picks.
    """
    unmapped: list[str] = []
    votes: dict[str, int] = {}
    for raw, n in found.musicbrainz:
        g = vocab.resolve(raw)
        if g:
            votes[g] = votes.get(g, 0) + max(n, 1)
        elif raw not in unmapped:
            unmapped.append(raw)
    wiki: list[str] = []
    for raw in found.wikipedia:
        g = vocab.resolve(raw)
        if g:
            if g not in wiki:
                wiki.append(g)
        elif raw not in unmapped:
            unmapped.append(raw)
    mb_top = max(votes, key=lambda g: votes[g]) if votes else None
    wiki_top = wiki[0] if wiki else None
    if mb_top and wiki_top:
        if mb_top == wiki_top:
            return Suggestion(mb_top, "both agree", unmapped)
        return Suggestion(mb_top, f"differ: Wikipedia says {wiki_top}", unmapped)
    if mb_top:
        return Suggestion(mb_top, "MusicBrainz only", unmapped)
    if wiki_top:
        return Suggestion(wiki_top, "Wikipedia only", unmapped)
    return Suggestion(None, "nothing in the vocabulary", unmapped)


# ── generate ─────────────────────────────────────────────────────────────────


def catalogue_acts(conn: sqlite3.Connection) -> list[tuple[str, str | None, int]]:
    """(artist, MBID, songs) for each act that files under its own name.

    A credit ("X feat. Y", "X & The Band" filed under X) takes its lead's
    genre (Grey, 2026-10-06), so only the lead is asked about.
    """
    from .artist_form import folder_artist

    cols = {r[1] for r in conn.execute("PRAGMA table_info(archive)")}
    mbid = "mb_artist_id" if "mb_artist_id" in cols else "NULL"
    mbname = "mb_artist_name" if "mb_artist_name" in cols else "NULL"
    acts: dict[str, list] = {}
    for artist, mb_id, mb_name, n in conn.execute(
        f"SELECT artist, {mbid}, {mbname}, COUNT(*) FROM archive "
        "WHERE status = 'CATALOGUED' AND artist IS NOT NULL AND trim(artist) != '' "
        f"GROUP BY artist, {mbid}, {mbname}"
    ):
        if folder_artist(artist, mb_name) != artist:
            continue
        a = acts.setdefault(artist, [None, 0, 0])
        if mb_id and n > a[2]:
            a[0], a[2] = mb_id, n
        a[1] += n
    return sorted(((k, v[0], v[1]) for k, v in acts.items()), key=lambda t: t[0].lower())


def _load_cache(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data: dict = json.load(fh)
            return data
    except (OSError, ValueError):
        return {}


def _write_aside(path: Path, write: Callable[[Path], None]) -> None:
    """Write beside *path*, then rename over it."""
    part = path.with_name(path.name + ".part")
    try:
        write(part)
        with open(part, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(part, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(part)
        raise


def _save_cache(path: Path, cache: dict) -> None:
    def write(p: Path) -> None:
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=1, sort_keys=True)

    _write_aside(path, write)


@dataclass
class Report:
    acts: int = 0
    asked: int = 0
    from_cache: int = 0
    unavailable: int = 0
    agree_with_law: int = 0
    no_suggestion: int = 0
    rows: int = 0
    unmapped: dict[str, int] = field(default_factory=dict)


def generate(
    conn: sqlite3.Connection,
    law,
    vocab: Vocabulary,
    cache_path: Path,
    out_path: Path,
    *,
    limit: int | None = None,
    refresh: bool = False,
    lookup: Callable[[str, str | None], Found] = look_up,
    progress: Callable[[str], None] = lambda s: None,
) -> Report:
    """Look each act up and write the review sheet. Reads the catalogue only."""
    rep = Report()
    cache = {} if refresh else _load_cache(cache_path)
    acts = catalogue_acts(conn)
    rep.acts = len(acts)
    rows: list[dict] = []
    for i, (artist, mbid, songs) in enumerate(acts, 1):
        hit = cache.get(artist)
        if hit is not None:
            found = Found(hit.get("mbid"), [tuple(x) for x in hit.get("musicbrainz", [])],
                          list(hit.get("wikipedia", [])), hit.get("note", ""))  # fmt: skip
            rep.from_cache += 1
        else:
            if limit is not None and rep.asked >= limit:
                continue
            try:
                found = lookup(artist, mbid)
            except LookupUnavailable as exc:
                rep.unavailable += 1
                progress(f"  ? {artist}: no answer ({exc})")
                continue
            rep.asked += 1
            cache[artist] = {
                "mbid": found.mbid,
                "musicbrainz": found.musicbrainz,
                "wikipedia": found.wikipedia,
                "note": found.note,
                "asked": datetime.date.today().isoformat(),
            }
            if rep.asked % 25 == 0:
                _save_cache(cache_path, cache)
                progress(f"  {i:,} of {len(acts):,} acts")
        s = suggest(found, vocab)
        for raw in s.unmapped:
            rep.unmapped[raw] = rep.unmapped.get(raw, 0) + 1
        current = law.genre_for(artist) or ""
        if s.genre is None:
            rep.no_suggestion += 1
            continue
        if current and law.agrees(artist, s.genre):
            rep.agree_with_law += 1
            continue
        rows.append(
            {
                "approve": "",
                "artist": artist,
                "songs": songs,
                "current_genre": current,
                "suggested_genre": s.genre,
                "agreement": s.agreement,
                "musicbrainz": "; ".join(f"{g} ({n})" for g, n in found.musicbrainz[:6]),
                "wikipedia": "; ".join(found.wikipedia[:6]),
                "not_in_vocabulary": "; ".join(s.unmapped[:6]),
                "notes": found.note,
            }
        )
    _save_cache(cache_path, cache)

    def write(p: Path) -> None:
        with open(p, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=SHEET_FIELDS)
            w.writeheader()
            w.writerows(rows)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_aside(out_path, write)
    rep.rows = len(rows)
    return rep


# ── apply ────────────────────────────────────────────────────────────────────


@dataclass
class Applied:
    changed: list[tuple[str, str, str]] = field(default_factory=list)  # artist, was, now
    added: list[tuple[str, str]] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    backup: Path | None = None


def apply(sheet: Path, law_path: Path, vocab: Vocabulary, *, dry_run: bool = False) -> Applied:
    """Load the approved rows of *sheet* into MasterLaw.csv.

    Only rows marked yes. The genre is the one in suggested_genre (Grey may
    have typed another); it must be in Genre_Allowed.txt, in any letter case.
    MasterLaw keeps its CRLF lines and every row not approved is left byte for
    byte. A backup is made first; the new file is written aside, read back
    and checked, then renamed over the old one.
    """
    from .canon.genre_law import GenreLaw

    res = Applied()
    with open(sheet, encoding="utf-8-sig", newline="") as fh:
        approved = [
            r for r in csv.DictReader(fh) if (r.get("approve") or "").strip().lower() in _APPROVED
        ]
    wanted: dict[str, tuple[str, str]] = {}  # law key -> (artist, genre)
    for r in approved:
        artist = (r.get("artist") or "").strip()
        typed = (r.get("suggested_genre") or "").strip()
        genre = vocab.canonical(typed)
        if not artist:
            continue
        if not genre:
            res.refused.append(
                f"{artist}: {typed!r} is not in Genre_Allowed.txt -- check the spelling"
                if typed
                else f"{artist}: approved, but no genre in suggested_genre"
            )
            continue
        wanted[GenreLaw._key(artist)] = (artist, genre)

    raw = law_path.read_bytes().decode("utf-8")
    lines = raw.split("\r\n")
    header, body = lines[0], lines[1:]
    seen: set[str] = set()
    out = [header]
    for line in body:
        if not line:
            out.append(line)
            continue
        row = next(csv.reader([line]))
        key = GenreLaw._key(row[0]) if row else ""
        if key in wanted and len(row) >= 2:
            artist, genre = wanted[key]
            seen.add(key)
            if row[1].strip() != genre:
                res.changed.append((row[0], row[1], genre))
                line = _csv_line([row[0], genre, *row[2:]])
        out.append(line)
    tail = out.pop() if out and out[-1] == "" else None
    for key, (artist, genre) in wanted.items():
        if key not in seen:
            res.added.append((artist, genre))
            out.append(_csv_line([artist, genre]))
    if tail is not None:
        out.append(tail)
    if dry_run or not (res.changed or res.added):
        return res

    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    res.backup = law_path.with_name(f"{law_path.name}.bak.pre_genre_suggestions_{stamp}")
    shutil.copy2(law_path, res.backup)
    new = "\r\n".join(out).encode("utf-8")

    def write(p: Path) -> None:
        p.write_bytes(new)
        check = GenreLaw(p)
        for _key, (artist, genre) in wanted.items():
            if check.genre_for(artist) != genre:
                raise ValueError(f"MasterLaw did not read back {artist!r} as {genre!r}")

    _write_aside(law_path, write)
    return res


def _csv_line(fields: list[str]) -> str:
    from io import StringIO

    buf = StringIO()
    csv.writer(buf, lineterminator="").writerow(fields)
    return buf.getvalue()


# ── Commands ─────────────────────────────────────────────────────────────────


def default_sheet() -> Path:
    return Path.home() / "Desktop" / f"MUSAEUS_genre_suggestions_{datetime.date.today()}.csv"


def cmd_generate(*, dry_run: bool, limit: int | None, refresh: bool, out: str | None) -> int:
    from .canon.genre_law import GenreLaw
    from .config import get_config
    from .network_policy import NetworkPolicy, set_policy

    cfg = get_config()
    vocab = Vocabulary(cfg.meta_dir / "Genre_Allowed.txt", cfg.meta_dir / "Genre_Canonical_Map.txt")
    if not vocab.allowed:
        print("ERROR: Genre_Allowed.txt is missing or empty -- nothing to map to.")
        return 1
    law = GenreLaw(cfg.meta_dir / "MasterLaw.csv")
    cache_path = cfg.meta_dir / "genre_suggestions_cache.json"
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    try:
        acts = catalogue_acts(conn)
        cached = _load_cache(cache_path)
        to_ask = [a for a, _m, _n in acts if refresh or a not in cached]
        if dry_run:
            print(f"\nMusaeus genre-suggest generate [DRY RUN]\n  acts: {len(acts):,}")
            print(f"  already looked up: {len(acts) - len(to_ask):,}")
            n = len(to_ask) if limit is None else min(limit, len(to_ask))
            print(f"  would look up: {n:,} (about {n * 2.5 / 60:.0f} min; nothing asked now)")
            return 0
        sheet = Path(out).expanduser() if out else default_sheet()
        print(
            f"\nMusaeus genre-suggest generate\n  acts: {len(acts):,}, to look up: {len(to_ask):,}"
        )
        set_policy(NetworkPolicy.ALLOWED)
        rep = generate(conn, law, vocab, cache_path, sheet, limit=limit, refresh=refresh,
                       progress=print)  # fmt: skip
    finally:
        conn.close()
    print(f"\n  looked up now: {rep.asked:,}   from earlier: {rep.from_cache:,}")
    if rep.unavailable:
        print(f"  no answer (asked again next time): {rep.unavailable:,}")
    print(f"  already agree with MasterLaw: {rep.agree_with_law:,}")
    print(f"  nothing in the vocabulary: {rep.no_suggestion:,}")
    print(f"  rows for you to decide: {rep.rows:,}")
    print(f"\n  Sheet: {sheet}")
    print("  Mark approve = yes on the rows you want (you may type another genre),")
    print(f'  then: musaeus genre-suggest apply "{sheet}"')
    if rep.unmapped:
        top = sorted(rep.unmapped.items(), key=lambda kv: -kv[1])[:12]
        print("\n  Genres the sources use that the vocabulary does not (most common):")
        for g, n in top:
            print(f"    {g}  ({n})")
    return 0


def cmd_apply(sheet: str, *, dry_run: bool) -> int:
    from .config import get_config
    from .masters_lock import EXIT_BUSY, MastersBusy, masters_lock

    cfg = get_config()
    path = Path(sheet).expanduser()
    if not path.exists():
        print(f"ERROR: no such sheet: {path}")
        return 1
    vocab = Vocabulary(cfg.meta_dir / "Genre_Allowed.txt", cfg.meta_dir / "Genre_Canonical_Map.txt")
    law_path = cfg.meta_dir / "MasterLaw.csv"
    if dry_run:
        res = apply(path, law_path, vocab, dry_run=True)
    else:
        # MasterLaw is read by GenreValidate at the start of Act 1: not
        # rewritten while a run or another writer holds the masters.
        try:
            with masters_lock(cfg.runs_root, exclusive=True, what="genre-suggest apply"):
                res = apply(path, law_path, vocab)
        except MastersBusy as exc:
            print(f"Not applied: {exc}")
            return EXIT_BUSY
    verb = "would change" if dry_run else "changed"
    print(f"\nMusaeus genre-suggest apply{' [DRY RUN]' if dry_run else ''}")
    for artist, was, now in res.changed:
        print(f"  {verb}: {artist}: {was} -> {now}")
    for artist, genre in res.added:
        print(f"  {'would add' if dry_run else 'added'}: {artist}: {genre}")
    for msg in res.refused:
        print(f"  NOT APPLIED: {msg}")
    if not (res.changed or res.added):
        print("  nothing to change")
    elif not dry_run:
        print(f"\n  MasterLaw backup: {res.backup}")
        print("  The library follows at the next Act 1 (GenreValidate).")
    return 1 if res.refused else 0
