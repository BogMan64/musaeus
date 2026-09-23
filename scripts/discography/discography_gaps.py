#!/usr/bin/env python3
"""Studio albums this library holds nothing from.

Not "you are missing track 7" but "you own five Springsteen records and Nebraska
is not one of them".

READ-ONLY on the library. Writes one CSV. Touches no audio file, and writes to no
MUSAEUS database.

SCOPE, per Grey's ruling: primary-type = Album with NO secondary types. See
discography/scope.py for what that excludes and what it deliberately gets wrong.

RUN IT DRY FIRST:

    python3 discography_gaps.py --dry-run

That resolves eligibility and artist MBIDs and reports what it WOULD ask, with no
network at all. Worth doing, because a live run costs one request per second per
artist and the eligibility thresholds are a judgement you may want to change
first.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from discography import match as match_mod  # noqa: E402
from discography import mb as mb_mod  # noqa: E402
from discography.library import (  # noqa: E402
    DEFAULT_MIN_ALBUMS,
    DEFAULT_MIN_TRACKS,
    MB_CACHE,
    VAULT_DB,
    OwnedArtist,
    load_artists,
)
from discography.match import Gap, find_gaps  # noqa: E402
from discography.mb import MusicBrainz, ReleaseGroupCache, Unavailable, allow_network  # noqa: E402
from discography.scope import partition  # noqa: E402


def log(msg: str = "") -> None:
    print(msg, flush=True)


CSV_COLUMNS = [
    "artist",
    "missing_studio_album",
    "year",
    "may_be_covered_by_compilation",
    "albums_owned_by_this_artist",
    "canonical_studio_albums",
    "release_group_mbid",
]


def write_csv(rows: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def gap_rows(artist: OwnedArtist, gaps: list[Gap], n_studio: int) -> list[dict]:
    return [
        {
            "artist": artist.artist,
            "missing_studio_album": g.album,
            "year": g.year or "",
            "may_be_covered_by_compilation": "yes" if g.may_be_covered_by_compilation else "",
            "albums_owned_by_this_artist": len(artist.albums),
            "canonical_studio_albums": n_studio,
            "release_group_mbid": g.mbid,
        }
        for g in gaps
    ]


def collect(
    mb: MusicBrainz, askable: list[OwnedArtist]
) -> tuple[list[dict], list[tuple[str, str]], list[str], dict[str, int]]:
    """Fetch and compare. (csv rows, lookup failures, complete artists, totals).

    `failed` is kept apart from `complete` on purpose. An artist whose lookup
    failed has an UNKNOWN discography, which is the opposite of a complete one.
    Counting them together would report "you own everything" about an artist
    nobody managed to ask about -- the most damaging thing this tool could say,
    and the reason mb.Unavailable exists rather than returning [].
    """
    rows: list[dict] = []
    failed: list[tuple[str, str]] = []
    complete: list[str] = []
    totals = {"studio": 0, "excluded": 0, "matched": 0, "gaps": 0}

    for i, a in enumerate(askable, 1):
        try:
            groups = mb.release_groups(a.mbid)
        except Unavailable as exc:
            failed.append((a.artist, str(exc)))
            log(f"[{i}/{len(askable)}] {a.artist}: UNAVAILABLE ({exc})")
            continue

        studio, dropped = partition(groups)
        gaps, matched = find_gaps(a.artist, a.albums, studio, a.has_compilation)
        totals["studio"] += len(studio)
        totals["excluded"] += len(dropped)
        totals["matched"] += len(matched)
        totals["gaps"] += len(gaps)

        log(
            f"[{i}/{len(askable)}] {a.artist}: {len(groups)} release group(s) -> "
            f"{len(studio)} studio; own {len(matched)}, missing {len(gaps)}"
        )
        if not gaps:
            complete.append(a.artist)
        rows.extend(gap_rows(a, gaps, len(studio)))

    return rows, failed, complete, totals


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=VAULT_DB)
    ap.add_argument("--mb-cache", type=Path, default=MB_CACHE)
    ap.add_argument("--status", default="CATALOGUED")
    ap.add_argument("--min-albums", type=int, default=DEFAULT_MIN_ALBUMS)
    ap.add_argument("--min-tracks", type=int, default=DEFAULT_MIN_TRACKS)
    ap.add_argument("--limit", type=int, default=0, help="max artists to ask about (0 = all)")
    ap.add_argument(
        "--out", type=Path, default=Path.home() / "Desktop" / "DISCOGRAPHY_gaps.csv"
    )
    ap.add_argument(
        "--cache", type=Path, default=None,
        help="SQLite cache for MusicBrainz results (default: ~/.cache/discography-lab/mb_cache.db)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve eligibility only, make no network request",
    )
    args = ap.parse_args(argv)

    artists, stats = load_artists(
        db=args.db,
        cache_db=args.mb_cache,
        status=args.status,
        min_albums=args.min_albums,
        min_tracks=args.min_tracks,
    )

    log(f"artists in library ({args.status}): {stats['artists_total']}")
    log(f"  excluded, under {args.min_tracks} tracks : {stats['excluded_too_few_tracks']}")
    log(f"  excluded, under {args.min_albums} albums : {stats['excluded_too_few_albums']}")
    log(f"  excluded, Classical genre          : {stats['excluded_classical']}")
    log(f"  album-oriented enough to ask about: {stats['eligible_before_mbid']}")
    log(f"    artist mbid already in archive  : {stats['mbid_from_archive']}")
    log(f"    artist mbid from MUSAEUS cache  : {stats['mbid_from_cache']}")
    log(f"    NO mbid, cannot ask             : {stats['unresolvable_no_mbid']}")
    log()
    log(match_mod.provenance())
    log(mb_mod.provenance())

    askable = [a for a in artists if a.mbid]
    unaskable = [a for a in artists if not a.mbid]
    if args.limit:
        askable = askable[: args.limit]

    if args.dry_run:
        log()
        log(f"DRY RUN -- no network. Would ask about {len(askable)} artist(s),")
        log(f"at least {len(askable) * mb_mod.RATE_LIMIT_S / 60:.1f} minute(s) at one request/sec.")
        log()
        log(f"{'artist':<30} {'owned':>5} {'trks':>5}  comp?")
        for a in askable[:25]:
            log(
                f"{a.artist[:30]:<30} {len(a.albums):>5} {a.track_count:>5}  "
                f"{'yes' if a.has_compilation else '-'}"
            )
        if len(askable) > 25:
            log(f"... and {len(askable) - 25} more")
        if unaskable:
            log()
            log(f"cannot ask, no artist mbid ({len(unaskable)}):")
            log("  " + ", ".join(a.artist for a in unaskable[:12]))
        return 0

    cache = ReleaseGroupCache(args.cache) if args.cache else ReleaseGroupCache()
    mb = MusicBrainz(cache=cache)
    # The whole fetch runs inside MUSAEUS's network gateway, in a `with` block so
    # the permissive policy is restored even if a lookup raises. Doing this by
    # hand with __enter__ would leave the process permissive on any exception --
    # the exact leak network_policy.policy() documents.
    with allow_network() as permission:
        log(permission)
        log()
        rows, failed, complete, totals = collect(mb, askable)

    write_csv(rows, args.out)
    mb.close()

    log()
    log(f"MusicBrainz requests            {mb.calls}  ({mb.cache_hits} served from cache)")
    log(f"cache: {cache.stats()}")
    log(f"release groups seen             {totals['studio'] + totals['excluded']}")
    log(f"  studio albums (the ruling)    {totals['studio']}")
    log(f"  excluded by the ruling        {totals['excluded']}")
    log(f"  of the studio albums, owned   {totals['matched']}")
    log(f"  MISSING                       {totals['gaps']}")
    assert totals["matched"] + totals["gaps"] == totals["studio"], (
        "every studio album must be either owned or missing"
    )
    log()
    log(f"complete discographies          {len(complete)}")
    if failed:
        log(f"UNKNOWN, lookup failed          {len(failed)}  (not 'complete')")
        for name, why in failed[:8]:
            log(f"    {name}: {why}")
    log()
    log(f"{len(rows)} row(s) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
