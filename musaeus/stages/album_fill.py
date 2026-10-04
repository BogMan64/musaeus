"""
MUSAEUS — Album Fill Stage (Enrichment, after AcoustID)

Fills an EMPTY album name, conservatively, from the strongest evidence first.
Grey, 2026-10-03: "please add it in where you suggest" -- the three
standalone scripts of 2026-09-21/23 (fill_albums_from_acoustid.py,
fill_albums_from_discogs.py, fill_albums_waterfall.py) took 2,192 albumless
tracks to 688 and were never wired in. This is them, as one stage, with
their rules unchanged:

  1. AcoustID   the recording's release groups, looked up with the
                fingerprint the AcoustID stage already stored (no fpcalc).
                Only a plain "Album" with no secondary type, crediting our
                artist; more than one -> MusicBrainz first-release date,
                earliest wins (one lookup per release group, cached).
  2. Discogs    artist + title text, only where AcoustID left it empty and
                credentials exist. Format must be Album and not Single, EP,
                Compilation, Unofficial, Promo or Sampler; the release must
                credit our artist; earliest year wins.
  3. Deezer     exact artist + exact title, studio albums only (no greatest
                hits, collections, live-at...), one unambiguous album.
  4. iTunes     the same strict rules (Grey, 2026-10-03): exact artist and
                title, no single/EP/compilation/reissue/live album, earliest
                release -- and only if MusicBrainz confirms the song is on that
                official studio album (iTunes alone picked "Built for Speed",
                dated 1974, for a 1981 Stray Cats song).

Text sources (2, 3) never try a title naming a specific version -- live,
remix, acoustic, demo, edit...: the searches ignore the qualifier and return
the studio album for a live take. A fingerprint knows which recording it
holds; text does not.

Anything ambiguous is LEFT EMPTY. A confidently wrong album is worse than a
missing one: it looks authoritative, moves the file into a wrong folder, and
nothing downstream can tell it was a guess. An album that exists is never
overwritten.

Each site is paced to its own published limit (AcoustID 3/s, MusicBrainz
1/s, Discogs 60/min, Deezer ~4/s), and a later site is asked only about what
the earlier ones could not settle. Grey asked for the free pass first: the
fingerprints are already stored, so nothing is fingerprinted again.

A row is stamped album_fill_checked_at once a source has ANSWERED -- filled,
or deliberately left empty -- and is not asked again. A row that got no
answer (network down, site refused) stays unstamped and is asked next run.
The album lives in the catalogue; the next Act 3's tagger writes it into the
master and organize files it into its album folder.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import unicodedata
import urllib.error
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..brackets import strip_bracketed
from ..context import RunContext, StageResult
from ..db import ensure_columns
from ..network_policy import check as _network_check
from .base import BaseStage

logger = logging.getLogger(__name__)

_ACOUSTID_URL = "https://api.acoustid.org/v2/lookup"
_MB_RG_URL = "https://musicbrainz.org/ws/2/release-group/{}?fmt=json"
_DISCOGS_URL = "https://api.discogs.com/database/search"
_DEEZER_URL = "https://api.deezer.com/search"
_ITUNES_URL = "https://itunes.apple.com/search"
_MB_RECORDING_URL = "https://musicbrainz.org/ws/2/recording/"
_UA = "MUSAEUS/1.0 ( musaeus-local )"

_AID_RATE_S = 0.34  # AcoustID: 3 requests/second
_MB_RATE_S = 1.1  # MusicBrainz: 1 request/second
_DISCOGS_RATE_S = 1.1  # Discogs: 60 requests/minute authenticated
_DEEZER_RATE_S = 0.25
_ITUNES_RATE_S = 3.0  # Apple publishes about 20 requests per minute
_TIMEOUT_S = 30
_COMMIT_EVERY = 25
_MB_CANDIDATES_MAX = 6  # cap MusicBrainz calls per track

_DISCOGS_BAD = {"single", "ep", "compilation", "unofficial release", "promo", "sampler"}
_VERSION_QUALIFIER = re.compile(
    r"\b(live|unplugged|in concert|remix|re-?recorded|acoustic|demo|"
    r"radio edit|single version|extended|instrumental|karaoke|reprise|"
    r"mono|stereo version|alternate)\b",
    re.I,
)
_DEEZER_BAD_ALBUM = re.compile(
    r"\b(greatest hits|best of|the collection|essential|anthology|"
    r"compilation|\blive\b|hits|vol\.? ?\d|now that|"
    r"ultimate|definitive|platinum collection|super hits)\b",
    re.I,
)


#: An iTunes album that is a reissue, not the original (Grey: the original wins).
_ITUNES_REISSUE = re.compile(
    r"\b(deluxe|remaster(ed)?|anniversary|expanded|re-?issue|bonus|special edition|"
    r"legacy edition|collector'?s)\b",
    re.I,
)


class Unavailable(Exception):
    """No answer was obtained: not 'nothing found', just not asked successfully."""


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = strip_bracketed(s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _get_json(url: str, headers: dict[str, str] | None = None) -> dict:
    _network_check(url)
    try:
        with urlopen(
            Request(url, headers={"User-Agent": _UA, **(headers or {})}), timeout=_TIMEOUT_S
        ) as r:
            data: dict = json.load(r)
            return data
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise Unavailable(str(exc)) from exc


# ── The network, one function per question (tests stand in for these) ──────


def acoustid_release_groups(fingerprint: str, duration: float, api_key: str) -> list[dict]:
    params = {
        "client": api_key,
        "fingerprint": fingerprint,
        "duration": str(int(duration)),
        "meta": "releasegroups",
        "format": "json",
    }
    data = _get_json(f"{_ACOUSTID_URL}?{urlencode(params)}")
    if data.get("status") != "ok":
        raise Unavailable(f"status={data.get('status')!r}")
    results = sorted(data.get("results") or [], key=lambda x: x.get("score", 0), reverse=True)
    return (results[0].get("releasegroups") or []) if results else []


def mb_first_release(release_group_id: str) -> str | None:
    return _get_json(_MB_RG_URL.format(release_group_id)).get("first-release-date") or None


def discogs_search(artist: str, title: str, key: str, secret: str) -> list[dict]:
    q = {"artist": artist, "track": title, "type": "release", "per_page": 25}
    auth = {"Authorization": f"Discogs key={key}, secret={secret}"}
    results: list[dict] = _get_json(f"{_DISCOGS_URL}?{urlencode(q)}", auth).get("results", [])
    return results


def deezer_search(artist: str, title: str) -> list[dict]:
    query = urlencode({"q": f"{artist} {title}", "limit": 10})
    items: list[dict] = _get_json(f"{_DEEZER_URL}?{query}").get("data", [])
    return items


def mb_confirm(artist: str, album: str, title: str) -> tuple[bool, str]:
    """Ask MusicBrainz whether this song is on an official studio album of this name.

    A name another source proposed is only a claim. MusicBrainz is asked the
    one question that settles it: is a recording of this title, credited to
    this artist, on an OFFICIAL release of that name whose group is a plain
    Album? Raises Unavailable when it cannot answer (a 503 is not "no").
    """
    q = lambda x: strip_bracketed(x).replace("\\", " ").replace('"', " ").strip()  # noqa: E731
    query = f'recording:"{q(title)}" AND artist:"{q(artist)}" AND release:"{q(album)}"'
    time.sleep(_MB_RATE_S)
    data = _get_json(
        f"{_MB_RECORDING_URL}?{urlencode({'query': query, 'fmt': 'json', 'limit': 10})}"
    )
    return choose_mb_confirmation(data.get("recordings", []), artist, title, album)


def itunes_search(artist: str, title: str) -> list[dict]:
    query = urlencode({"term": f"{artist} {title}", "entity": "song", "limit": 25})
    results: list[dict] = _get_json(f"{_ITUNES_URL}?{query}").get("results", [])
    return results


# ── The choices, pure (the rules of the 2026-09 scripts, unchanged) ─────────


def choose_from_release_groups(
    groups: list[dict], artist: str, first_release=mb_first_release
) -> tuple[str | None, str]:
    clean = [g for g in groups if g.get("type") == "Album" and not g.get("secondarytypes")]
    if not clean:
        return None, "no plain Album among candidates"
    want = norm(artist).split(" ")[0] if artist else ""
    if want:
        matched = [
            g for g in clean if any(want in norm(a.get("name")) for a in (g.get("artists") or []))
        ]
        if matched:
            clean = matched
        elif len(clean) > 1:
            return None, "no candidate credits our artist"
    if len(clean) == 1:
        return clean[0].get("title"), "single clean Album"
    dated = []
    for g in clean[:_MB_CANDIDATES_MAX]:
        d = first_release(g["id"])
        if d:
            dated.append((d, g.get("title")))
    if not dated:
        return None, f"{len(clean)} candidates, no release dates"
    dated.sort()
    if len(dated) > 1 and dated[0][0] == dated[1][0]:
        return None, "earliest date is a tie"
    return dated[0][1], f"earliest of {len(dated)} ({dated[0][0]})"


def choose_from_discogs(results: list[dict], artist: str) -> tuple[str | None, str]:
    want = norm(artist).split(" ")[0] if artist else ""
    cands = []
    for x in results:
        fmts = {f.lower() for f in (x.get("format") or [])}
        if "album" not in fmts or (fmts & _DISCOGS_BAD):
            continue
        title = x.get("title") or ""
        if " - " not in title:
            continue
        who, album = title.split(" - ", 1)
        if want and want not in norm(who):
            continue
        try:
            year: int | None = int(str(x.get("year") or ""))
        except ValueError:
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
    if len({c[1] for c in cands if c[0] == cands[0][0]}) > 1:
        return None, f"{len(names)} candidates, earliest year is a tie"
    return cands[0][1], f"earliest of {len(names)} ({cands[0][0]})"


def choose_from_deezer(items: list[dict], artist: str, title: str) -> tuple[str | None, str]:
    want_a, want_t = norm(artist), norm(title)
    names: list[str] = []
    for t in items:
        if norm((t.get("artist") or {}).get("name")) != want_a or norm(t.get("title")) != want_t:
            continue
        if _VERSION_QUALIFIER.search(t.get("title") or ""):
            continue  # norm() drops brackets: "Rosanna (Live)" would match "Rosanna"
        alb = (t.get("album") or {}).get("title") or ""
        if not alb or _DEEZER_BAD_ALBUM.search(alb) or norm(alb) == want_t:
            continue
        names.append(alb)
    if not names:
        return None, "no studio album for an exact artist+title match"
    uniq = list(dict.fromkeys(names))
    if len(uniq) == 1:
        return uniq[0], "unambiguous"
    return None, f"{len(uniq)} candidate albums"


def choose_from_itunes(items: list[dict], artist: str, title: str) -> tuple[str | None, str]:
    """iTunes, under the same strict rules as the others.

    Exact artist and exact title (a "(Live)" or "(Single Version)" title is a
    different recording and does not match); an album that is a single, EP,
    compilation or reissue is skipped; the EARLIEST release wins; two albums
    with the same earliest date are a tie and the row is left empty.
    """
    want_a, want_t = norm(artist), norm(title)
    dated: list[tuple[str, str]] = []
    for t in items:
        if norm(t.get("artistName")) != want_a or norm(t.get("trackName")) != want_t:
            continue
        if _VERSION_QUALIFIER.search(t.get("trackName") or ""):
            continue  # norm() drops brackets: "Rosanna (Live)" would match "Rosanna"
        alb = (t.get("collectionName") or "").strip()
        if not alb or norm(alb) == want_t:
            continue
        if _DEEZER_BAD_ALBUM.search(alb) or _ITUNES_REISSUE.search(alb):
            continue
        if re.search(r"\s-\s(single|ep)$", alb, re.I) or (t.get("trackCount") or 0) < 4:
            continue
        dated.append(((t.get("releaseDate") or "9999")[:10], alb))
    if not dated:
        return None, "no studio album for an exact artist+title match"
    names = {a for _, a in dated}
    if len(names) == 1:
        return dated[0][1], "unambiguous"
    dated.sort()
    if len({a for d, a in dated if d == dated[0][0]}) > 1:
        return None, f"{len(names)} candidate albums, earliest date is a tie"
    return dated[0][1], f"earliest of {len(names)} ({dated[0][0]})"


def discogs_confirm(artist: str, album: str, title: str, key: str, secret: str) -> tuple[bool, str]:
    """A second opinion: does Discogs list an Album release of this name by this artist?

    Weaker than MusicBrainz's answer: Discogs' search filters on the song
    title but does not return the tracklist, so this says the album exists
    under that artist and the search found the song with it. Raises
    Unavailable when it cannot answer.
    """
    q = {
        "artist": artist,
        "release_title": album,
        "track": title,
        "type": "release",
        "per_page": 25,
    }
    auth = {"Authorization": f"Discogs key={key}, secret={secret}"}
    time.sleep(_DISCOGS_RATE_S)
    results = _get_json(f"{_DISCOGS_URL}?{urlencode(q)}", auth).get("results", [])
    return choose_discogs_confirmation(results, artist, album)


def choose_discogs_confirmation(results: list[dict], artist: str, album: str) -> tuple[bool, str]:
    """Pure: an Album-format release titled *album*, credited to *artist*, not a single/EP/compilation."""
    first = norm(artist).split(" ")[0] if artist else ""
    for x in results:
        fmts = {f.lower() for f in (x.get("format") or [])}
        if "album" not in fmts or (fmts & _DISCOGS_BAD):
            continue
        title = x.get("title") or ""
        if " - " not in title:
            continue
        who, name = title.split(" - ", 1)
        if first in norm(who) and norm(name) == norm(album):
            return True, f"Discogs: lists the album '{name.strip()}' ({x.get('year') or '?'})"
    return False, "Discogs could not confirm an album of that name by this artist"


def choose_mb_confirmation(
    recordings: list[dict], artist: str, title: str, album: str
) -> tuple[bool, str]:
    """Pure: does any recording carry *title* by *artist* on an official studio album *album*?"""
    want_a, want_t, want_alb = norm(artist), norm(title), norm(album)
    first = want_a.split(" ")[0] if want_a else ""
    for rec in recordings:
        credit = norm(" ".join(c.get("name", "") for c in rec.get("artist-credit") or []))
        if rec.get("score", 0) < 90 or norm(rec.get("title")) != want_t or first not in credit:
            continue
        for rel in rec.get("releases") or []:
            rg = rel.get("release-group") or {}
            if (
                norm(rel.get("title")) == want_alb
                and rel.get("status") == "Official"
                and rg.get("primary-type") == "Album"
                and not rg.get("secondary-types")
            ):
                year = (rel.get("date") or "?")[:4]
                return True, f"MusicBrainz: on the official album '{rel['title']}' ({year})"
    return (
        False,
        "MusicBrainz could not confirm this song is on an official studio album of that name",
    )


def _ensure_columns(conn) -> None:  # type: ignore[type-arg]
    """The column this stage owns, beside the code that reads it."""
    ensure_columns(conn, (("album_fill_checked_at", "TEXT"),))


#: An album that is not an album. Grey, 2026-10-03 ("yes"): a source folder's
#: playlist name -- "My playlist S", 1,841 songs in the USB1 batch -- counts
#: as no album, so a real one may replace it. Nothing else is ever replaced.
NO_ALBUM_SQL = "(album IS NULL OR TRIM(album)='' OR album LIKE 'My playlist%')"

_CANDIDATES = (
    f"FROM archive WHERE status='CATALOGUED' AND {NO_ALBUM_SQL} "
    "AND (album_fill_checked_at IS NULL OR album_fill_checked_at='')"
)


class AlbumFillStage(BaseStage):
    """Album Fill — an empty album from AcoustID, then Discogs, then Deezer."""

    NAME = "album-fill"

    @classmethod
    def plan_candidates(cls, conn, cfg) -> tuple[int, str]:
        """Rows this stage would act on. Read-only; see planner.py."""
        what = "catalogued rows with no album, not yet asked"
        for query in (
            f"SELECT COUNT(*) {_CANDIDATES}",
            # album_fill_checked_at arrives on the first real run
            f"SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' AND {NO_ALBUM_SQL}",
        ):
            try:
                return int(conn.execute(query).fetchone()[0]), what
            except sqlite3.OperationalError:
                continue
        return 0, what  # a catalogue without an album column has nothing to fill

    def validate(self, ctx: RunContext) -> None:
        if not ctx.config.acousticid_api_key:
            logger.warning("[album-fill] no AcoustID key: only Discogs and Deezer will be asked")

    def _fill(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        if dry_run:
            n, what = self.plan_candidates(ctx.conn, ctx.config)
            result.files_processed = n
            result.notes.append(f"[DRY RUN] {n:,} {what}; no site asked")
            ctx.record_stage(result)
            return result

        from .acousticid import _ensure_columns as _fingerprint_columns

        _fingerprint_columns(ctx.conn)  # read below; AcoustID may never have run here
        _ensure_columns(ctx.conn)
        cfg = ctx.config
        aid_key = cfg.acousticid_api_key
        dg_key, dg_secret = cfg.discogs_consumer_key, cfg.discogs_consumer_secret
        rows = ctx.conn.execute(
            f"SELECT id, file_path, artist, title, chromaprint, chromaprint_duration {_CANDIDATES} "
            "ORDER BY artist, title"
        ).fetchall()
        mb_cache: dict[str, str | None] = {}

        def first_release(rg_id: str) -> str | None:
            if rg_id not in mb_cache:
                time.sleep(_MB_RATE_S)
                mb_cache[rg_id] = mb_first_release(rg_id)
            return mb_cache[rg_id]

        tally: dict[str, int] = {}
        for i, row in enumerate(rows, 1):
            result.files_processed += 1
            artist, title = (row["artist"] or "").strip(), (row["title"] or "").strip()
            album, source, reason, answered = None, "", "", False
            try:
                if aid_key and row["chromaprint"] and row["chromaprint_duration"]:
                    time.sleep(_AID_RATE_S)
                    groups = acoustid_release_groups(
                        row["chromaprint"], row["chromaprint_duration"], aid_key
                    )
                    album, reason = (
                        choose_from_release_groups(groups, artist, first_release)
                        if groups
                        else (None, "no AcoustID release groups")
                    )
                    source, answered = "acoustid", True
                text_ok = artist and title and not _VERSION_QUALIFIER.search(title)
                if album is None and text_ok and dg_key and dg_secret:
                    time.sleep(_DISCOGS_RATE_S)
                    album, reason = choose_from_discogs(
                        discogs_search(artist, title, dg_key, dg_secret), artist
                    )
                    source, answered = "discogs", True
                if album is None and text_ok:
                    time.sleep(_DEEZER_RATE_S)
                    album, reason = choose_from_deezer(deezer_search(artist, title), artist, title)
                    source, answered = "deezer", True
                if album is None and text_ok:
                    time.sleep(_ITUNES_RATE_S)
                    album, reason = choose_from_itunes(itunes_search(artist, title), artist, title)
                    source, answered = "itunes", True
                    if album is not None:
                        # iTunes' catalogue is full of compilations and placeholder dates:
                        # its answer is only a claim until MusicBrainz agrees.
                        confirmed, evidence = mb_confirm(artist, album, title)
                        reason = f"{reason}; {evidence}"
                        album = album if confirmed else None
            except Unavailable as exc:
                # Not an answer: leave the row unstamped, ask again next run.
                result.files_skipped += 1
                tally["no answer (asked again next run)"] = (
                    tally.get("no answer (asked again next run)", 0) + 1
                )
                logger.warning("[album-fill] %s: %s", row["file_path"], exc)
                continue
            if not answered:
                # Nothing could be asked: no stored fingerprint, and a title
                # naming a version is never searched by text. Not stamped, so a
                # fingerprint stored later still gets its turn.
                result.files_skipped += 1
                tally["nothing to ask"] = tally.get("nothing to ask", 0) + 1
                continue

            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if album:
                changed = ctx.conn.execute(
                    "UPDATE archive SET album=?, album_fill_checked_at=? "
                    f"WHERE id=? AND {NO_ALBUM_SQL}",
                    (album, now, row["id"]),
                ).rowcount
                if changed:
                    result.files_changed += 1
                    ctx.log_event("ALBUM_FILLED", file_path=row["file_path"], new_value=album,
                                  stage=self.NAME, note=f"{source}: {reason}")  # fmt: skip
                    tally[f"filled from {source}"] = tally.get(f"filled from {source}", 0) + 1
            else:
                ctx.conn.execute(
                    "UPDATE archive SET album_fill_checked_at=? WHERE id=?", (now, row["id"])
                )
                ctx.log_event("ALBUM_LEFT_EMPTY", file_path=row["file_path"], stage=self.NAME,
                              note=f"{source or 'none'}: {reason}")  # fmt: skip
                tally["left empty"] = tally.get("left empty", 0) + 1
            if i % _COMMIT_EVERY == 0:
                ctx.conn.commit()
        ctx.conn.commit()
        for k, v in sorted(tally.items(), key=lambda kv: -kv[1]):
            result.notes.append(f"{v:,} {k}")
        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._fill(ctx, dry_run=True)

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """A row the stage says it filled must have that album now."""
        rows = ctx.conn.execute(
            "SELECT a.file_path, a.album, e.new_value FROM archive a JOIN events e "
            "ON e.file_path = a.file_path WHERE e.run_id = ? AND e.event_type = 'ALBUM_FILLED' "
            "ORDER BY e.id DESC LIMIT 10",
            (ctx.run_id,),
        ).fetchall()
        wrong = [r["file_path"] for r in rows if (r["album"] or "") != (r["new_value"] or "")]
        return (
            [f"{len(wrong)} filled row(s) do not carry the album written: {wrong[:3]}"]
            if wrong
            else []
        )

    def run(self, ctx: RunContext) -> StageResult:
        return self._fill(ctx, dry_run=False)
