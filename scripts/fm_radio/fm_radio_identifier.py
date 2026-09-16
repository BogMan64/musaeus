#!/usr/bin/env python3
"""FM radio version identifier -- which pressing is the one your ears remember.

    python3 fm_radio_identifier.py --limit 20 --out ~/Desktop/fm_radio_review.csv
    python3 fm_radio_identifier.py --screensaver          # runs only while idle
    python3 fm_radio_identifier.py --dry-run              # cache only, no network

READS the MUSAEUS library. WRITES nothing but a CSV and its own cache.
No database is modified, no tag is touched, no file is moved. It proposes into
a CSV and a human rules -- the same shape as every other review artefact here.

TWO PASSES, because the two sources cost very different amounts.

  PASS 1  ListenBrainz, ONE request per artist, and it returns the artist's
          whole catalogue already ranked by listen count. Measured on The
          Beatles: 9,057 records, "Let It Be" first at 2,093,966 listens. So
          one call can narrow the field for every song by that artist at once.

  PASS 2  MusicBrainz, one request per SONG, and only for songs pass 1 could
          not settle. This is the expensive half -- one call per second -- so
          the point of pass 1 is to make pass 2 small.

WHAT IT CANNOT DO, stated plainly because the wishlist asks for it: prefer the
pressing that "actually charted". No chart data exists in MusicBrainz, and the
obvious substitute is worse than nothing -- billboard-charts' own documentation
warns that Billboard returns HTTP 200 with plausible markup for weeks that
never existed, stamped with whatever date you asked for. Fabricated history
that looks complete is the one output this project cannot tolerate. So charting
is approximated by "was released as a Single" plus "was it first" plus "is it
the one people play", and the CSV says which of the three it actually had.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fmradio.cache import Cache  # noqa: E402
from fmradio.clients import (  # noqa: E402
    DEFAULT_MIN_LISTENS,
    AuthRequired,
    ListenBrainz,
    MusicBrainz,
    SourceUnavailable,
    candidate_from_listenbrainz,
    candidate_from_musicbrainz,
)
from fmradio.idle import IdleGate  # noqa: E402
from fmradio.model import Proposal  # noqa: E402
from fmradio.popularity import DEFAULT_QUANTILE, apply_floor, artist_floor  # noqa: E402
from fmradio.report import write as write_csv  # noqa: E402
from fmradio.score import rank  # noqa: E402
from fmradio.secrets import TokenMissing, load_token  # noqa: E402
from fmradio.titles import base_title, provenance  # noqa: E402

VAULT_DB = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/musaeus.db")
MB_CACHE = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/_db_backups/mb_cache.db")
DEFAULT_CACHE = Path.home() / ".cache" / "fm-radio-lab" / "cache.db"

#: What the library actually holds, keyed (artist, title) lowercased. Populated
#: by library_songs() so the report can say "you hold the 1987 edition of a 1965
#: recording" rather than only naming a candidate.
HELD: dict[tuple[str, str], dict] = {}


def log(msg: str) -> None:
    print(msg, flush=True)


def library_songs(limit: int, *, only_multi: bool) -> list[tuple[str, str, str]]:
    """(artist, title, artist_mbid) for songs to ask about.

    TWO MODES, because there are two questions and only one of them needs
    duplicates.

    only_multi=True  -- songs held more than once. "Which of these copies is
                        the radio version?" The wishlist's stated scope.

    only_multi=False -- every song. "Is the ONE copy I kept the right version?"
                        Which is the more useful question on a deduped library:
                        measured 2026-09-10, after a fresh re-ingest of 2,441
                        files, the vault held 2,140 CATALOGUED rows and ZERO
                        songs under more than one artist+title, because the 294
                        duplicates were still sitting in DUPE_REVIEW. In
                        multi-only mode this tool correctly found nothing to do,
                        which is a right answer to a narrow question.

    Both databases are opened mode=ro. Nothing here writes to the vault.
    """
    if not VAULT_DB.exists():
        log(f"no vault database at {VAULT_DB}")
        return []

    conn = sqlite3.connect(f"file:{VAULT_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(archive)")}
        select_mbid = "mb_artist_id" if "mb_artist_id" in cols else "NULL"
        # original_year is MUSAEUS's own recovered recording year -- read, never
        # re-derived. Its docstring records why it exists: `year` is the year of
        # the EDITION held, so 221 of 384 Rock & Roll tracks were dated 2010 or
        # later, and Beach Boys "409" (1962) carried year = 2012.
        select_oy = "MIN(original_year)" if "original_year" in cols else "NULL"
        having = "HAVING n > 1" if only_multi else ""
        rows = conn.execute(
            f"""
            SELECT artist, title,
                   MAX({select_mbid}) AS artist_mbid,
                   {select_oy}        AS original_year,
                   MIN(year)          AS edition_year,
                   MIN(duration)      AS duration,
                   COUNT(*)           AS n
            FROM archive
            WHERE status = 'CATALOGUED'
              AND artist IS NOT NULL AND trim(artist) != ''
              AND title  IS NOT NULL AND trim(title)  != ''
            GROUP BY lower(trim(artist)), lower(trim(title))
            {having}
            ORDER BY n DESC, artist, title
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        global HELD  # local metadata, so the CSV can show what you actually own
        HELD = {
            (r["artist"].strip().lower(), r["title"].strip().lower()): {
                "original_year": r["original_year"],
                "edition_year": r["edition_year"],
                "duration": r["duration"],
                "copies": r["n"],
            }
            for r in rows
        }
    finally:
        conn.close()

    out = [(r["artist"], r["title"], r["artist_mbid"] or "") for r in rows]

    # Fill missing artist mbids from the MusicBrainz cache MUSAEUS already
    # built -- 2,644 resolved artists as of 2026-09-10. Re-resolving them would
    # be one MusicBrainz call each for work already done.
    missing = {a for a, _t, m in out if not m}
    if missing and MB_CACHE.exists():
        c2 = sqlite3.connect(f"file:{MB_CACHE}?mode=ro", uri=True)
        c2.row_factory = sqlite3.Row
        try:
            lookup = {
                (r["artist_key"] or "").lower(): r["mbid"]
                for r in c2.execute(
                    "SELECT artist_key, mbid FROM mb_artist "
                    "WHERE found = 1 AND mbid IS NOT NULL AND trim(mbid) != ''"
                )
            }
        finally:
            c2.close()
        out = [
            (a, t, m or lookup.get(a.strip().lower(), "")) for a, t, m in out
        ]
    return out


def score_songs(
    entries: list[tuple[str, str, str]],
    by_artist: dict[str, list[dict]],
    cache,
    quantile: float,
    use_musicbrainz: bool = False,
) -> tuple[list[Proposal], list[tuple[str, str, str]]]:
    """Score songs from cached data. (proposals, the ones still unsettled).

    Pure with respect to the network: everything it reads is already in
    by_artist or the cache. That is what lets pass 2 call it a second time in
    the same run instead of asking the user to re-run.
    """
    proposals: list[Proposal] = []
    unsettled: list[tuple[str, str, str]] = []

    for artist, title, mbid in entries:
        records = by_artist.get(mbid, [])

        # The floor is computed from the artist's WHOLE catalogue, not from the
        # pressings of this one song. A song with three pressings has no usable
        # distribution of its own; the artist's does.
        floor = artist_floor([r.get("total_listen_count") for r in records], q=quantile)

        want = base_title(title)
        matches = [r for r in records if base_title(r.get("recording_name", "")) == want]
        kept, floor_waived = apply_floor(
            matches, floor, lambda r: r.get("total_listen_count")
        )
        candidates = [candidate_from_listenbrainz(r) for r in kept]

        if use_musicbrainz:
            for rec in cache.get_song(artist, title) or []:
                for rel in rec.get("releases") or []:
                    candidates.append(candidate_from_musicbrainz(rec, rel))

        p = rank(artist, title, candidates)
        # Annotate with what is actually held, so a reviewer can see the gap
        # rather than only the recommendation. This is the whole value in
        # verify mode: "you hold a 2012 edition of a 1962 recording".
        held = HELD.get((artist.strip().lower(), title.strip().lower()))
        if held and p.best is not None:
            recommended = p.best.candidate.year
            mine = held.get("original_year") or held.get("edition_year")
            try:
                mine_year = int(str(mine)[:4]) if mine else None
            except ValueError:
                mine_year = None
            if recommended and mine_year and mine_year > recommended + 1:
                p.best.reasons.append(
                    f"  ! you hold a {mine_year} edition of a {recommended} recording"
                )
        if p.best is not None:
            p.best.reasons.append(f"  {floor.describe()}")
            if floor_waived:
                # Surfaced rather than swallowed: it means the song sits below
                # its own artist's top decile, which is ordinary for an album
                # cut and is also the shape of a title-matching failure.
                p.best.reasons.append(
                    "  ! popularity floor waived -- every pressing of this song is "
                    "below the artist's top decile"
                )
        if not p.confident:
            unsettled.append((artist, title, mbid))
        proposals.append(p)

    return proposals, unsettled


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=25, help="songs to consider")
    ap.add_argument("--out", type=Path, default=Path.home() / "Desktop" / "fm_radio_review.csv")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument(
        "--min-listens",
        type=int,
        default=DEFAULT_MIN_LISTENS,
        help="absolute noise gate at fetch time. The real separation of pressings "
        "from bootlegs is the per-artist floor, see --quantile",
    )
    ap.add_argument(
        "--quantile",
        type=float,
        default=DEFAULT_QUANTILE,
        help="per-artist popularity floor as a quantile of that artist's own "
        "listen counts (default %(default)s = top decile). Lower it to consider "
        "more obscure pressings",
    )
    ap.add_argument(
        "--screensaver",
        action="store_true",
        help="only make requests while the machine has been idle 2 minutes",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="use only what is cached; make no network request at all",
    )
    ap.add_argument("--pass2", action="store_true", help="ask MusicBrainz for unsettled songs")
    ap.add_argument(
        "--only-multi",
        action="store_true",
        help="only songs held more than once (choose between copies). Default is "
        "every song (is the copy I kept the right version?)",
    )
    args = ap.parse_args()

    songs = library_songs(args.limit, only_multi=args.only_multi)
    if not songs:
        log(
            "nothing to ask about."
            + (
                "  No song is held more than once -- try without --only-multi to "
                "check whether the single copy you kept is the right version."
                if args.only_multi
                else "  The library has no catalogued songs."
            )
        )
        return 0
    mode = "held more than once" if args.only_multi else "all catalogued songs"
    log(f"{len(songs)} song(s) to consider ({mode})")
    # Which title-matching rule is actually in force. Printed every run because
    # the MUSAEUS import is allowed to fail, and a run that quietly fell back to
    # the crude matcher gives worse answers with no outward sign.
    log(provenance())

    cache = Cache(args.cache)
    gate = IdleGate() if args.screensaver else None
    if args.screensaver:
        log(
            "screensaver mode: "
            + ("idle detection available" if gate.available() else "NO idle detection here -- running unthrottled")
        )

    lb = None
    if not args.dry_run:
        try:
            lb = ListenBrainz(token=load_token(), min_listens=args.min_listens)
        except (TokenMissing, AuthRequired) as exc:
            log(f"\nCannot reach ListenBrainz:\n{exc}\n")
            return 2

    mb = MusicBrainz() if (args.pass2 and not args.dry_run) else None

    # ── PASS 1
    by_artist: dict[str, list[dict]] = {}
    for artist, _title, mbid in songs:
        if not mbid or mbid in by_artist:
            continue
        cached = cache.get_artist(mbid)
        if cached is not None:
            by_artist[mbid] = cached
            continue
        if lb is None:
            continue
        if gate:
            gate.wait_until_idle(log)
        try:
            records = lb.top_recordings(mbid)
        except AuthRequired as exc:
            # Fatal, never skipped. A 401 treated as "no data" would let every
            # artist score as "no ListenBrainz signal" and the run would report
            # success having measured nothing.
            log(f"\nAUTHENTICATION FAILED -- stopping.\n{exc}\n")
            return 2
        except SourceUnavailable as exc:
            cache.note_failure("lb_artist", mbid, str(exc))
            log(f"  {artist}: unavailable ({exc})")
            continue
        cache.put_artist(mbid, records)
        by_artist[mbid] = records
        log(f"  {artist}: {len(records)} recording(s) over {args.min_listens} listen(s)")

    # ── SCORE (pass 1)
    proposals, needs_pass2 = score_songs(songs, by_artist, cache, args.quantile)

    # ── PASS 2, only the unsettled -- fetched AND re-scored in this same run.
    #
    # This used to fetch the pressings, cache them, and print "re-run to score
    # the newly cached pressings". Two invocations to get one answer, and the
    # first one always ended by telling you it had not finished. Worse, the
    # second run's behaviour depended on cache state rather than its arguments,
    # so the same command produced different output on consecutive runs -- and
    # anyone who took the first CSV at face value read a verdict reached without
    # the MusicBrainz evidence the run had just gone and fetched.
    #
    # Scoring is pure and cheap (fmradio/score.py touches no network), so the
    # only reason to defer it was the structure. Now the unsettled songs are
    # re-scored in place from the freshly-cached pressings and one run gives the
    # final CSV.
    if mb is not None and needs_pass2:
        log(f"\npass 2: asking MusicBrainz about {len(needs_pass2)} unsettled song(s)")
        fetched = 0
        for artist, title, _mbid in needs_pass2:
            if cache.get_song(artist, title) is not None:
                continue
            if gate:
                gate.wait_until_idle(log)
            try:
                recs = mb.pressings(artist, title)
            except SourceUnavailable as exc:
                cache.note_failure("mb_song", f"{artist}\x1f{title}", str(exc))
                log(f"  {artist} - {title}: unavailable ({exc})")
                continue
            cache.put_song(artist, title, recs)
            fetched += 1
            log(f"  {artist} - {title}: {len(recs)} pressing(s)")

        log(f"\nre-scoring {len(needs_pass2)} song(s) with the new pressings")
        rescored, still_unsettled = score_songs(
            needs_pass2, by_artist, cache, args.quantile, use_musicbrainz=True
        )
        # Replace in place, preserving the original row order. Keyed on
        # (artist, title) folded the same way HELD is, so a stray difference in
        # case or spacing cannot cause a silent failure to substitute -- which
        # would look exactly like pass 2 having achieved nothing.
        replacement = {(p.artist.strip().lower(), p.title.strip().lower()): p for p in rescored}
        settled_now = 0
        for i, p in enumerate(proposals):
            key = (p.artist.strip().lower(), p.title.strip().lower())
            if key in replacement:
                if replacement[key].confident and not p.confident:
                    settled_now += 1
                proposals[i] = replacement[key]
        log(
            f"  {fetched} song(s) newly fetched; "
            f"{settled_now} settled by pass 2; {len(still_unsettled)} still need a ruling"
        )

    counts = write_csv(proposals, args.out)
    log(
        f"\n{counts['rows']} row(s) -> {args.out}\n"
        f"  confident      : {counts['confident']}\n"
        f"  needs a ruling : {counts['undecided']}\n"
        f"  only one cand. : {counts['only_one']}\n"
        f"  popularity only: {counts['no_date']}\n"
        f"  no candidates  : {counts['no_candidates']}\n"
        f"cache: {cache.stats()}"
    )
    cache.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
