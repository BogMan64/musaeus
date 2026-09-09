#!/usr/bin/env python3
"""
Put MusicBrainz artist IDs onto the FILES, for a library already on disk.

The problem this fixes
----------------------
Measured 2026-08-29: of 10,588 finalised files, **732** carried a
MusicBrainz ID and 9,856 did not. The ~8,074 MBIDs the 08-27 campaign
resolved lived in `musaeus.db`, which is treated as transient per-batch
state and has since been reset to 87 rows. They are gone.

That is the whole argument for identity belonging on disk. `IdentityTagStage`
does this correctly on ingest, but it reads `archive` rows, and there are no
archive rows for this library any more. So this reads artists from the FILES
and writes identity back to the FILES, and never needs the database at all.

Where the answers come from
---------------------------
`mb_cache.db` survived the reset, holding 2,997 artist lookups (2,158 found,
839 settled misses). Measured against the library that covers **7,689 files
with no network request at all**; only 35 files -- 34 distinct artists --
need MusicBrainz asked. A further 2,132 files name artists MusicBrainz has
already said it does not have: those are not failures to retry, they are the
population AcoustID exists for.

    7,689  cache hit         free, offline
    2,132  cached miss       MB has no such artist -> AcoustID's job
      732  already tagged
       35  network lookup    ~1 minute at the 1 req/s courtesy rate

Three states, not two
---------------------
Finding #13, and #15 which was its own fix reintroducing it one level down:
a nullable column used as both a result and a to-do marker cannot express
"asked, and the answer was no". So `found=0` in the cache is an ANSWER and is
never re-queried here, while a `LookupUnavailable` -- the request not
completing -- touches nothing and leaves the artist for a later run.

Safety
------
Writes go through `musaeus.identity_tags.write_identity`, which reads every
value back off disk in a fresh handle before reporting success. Only freeform
identity atoms are touched; no audio stream is read or re-encoded, so
`audio_hash` is unchanged by construction. Each journal record is fsynced
BEFORE its file is touched, so an interrupted run stays reversible.

Usage:
    python3 scripts/backfill_identity_tags.py                    # dry run
    python3 scripts/backfill_identity_tags.py --apply            # cache only
    python3 scripts/backfill_identity_tags.py --apply --network  # + the 35
    python3 scripts/backfill_identity_tags.py --undo JOURNAL.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.identity_tags import read_identity, write_identity  # noqa: E402
from musaeus.network_policy import NetworkPolicy, policy  # noqa: E402

_AUDIO_SUFFIXES = (".m4a", ".mp4", ".alac", ".flac")
_MB_COURTESY_S = 1.05  # MusicBrainz asks for <=1 req/s from anonymous clients


# ── inputs ────────────────────────────────────────────────────────────────────


def read_artist(path: Path) -> str | None:
    """The artist as the FILE carries it. None when the file cannot be read."""
    suffix = path.suffix.lower()
    try:
        if suffix in (".m4a", ".mp4", ".alac"):
            from mutagen.mp4 import MP4  # type: ignore[import-untyped]

            tags: Any = MP4(str(path)).tags or {}
            v = tags.get("\xa9ART")
            return str(v[0]).strip() if v else ""
        if suffix == ".flac":
            from mutagen.flac import FLAC  # type: ignore[import-untyped]

            v = FLAC(str(path)).get("artist")
            return str(v[0]).strip() if v else ""
    except Exception:
        return None
    return None


def load_cache(cache_path: Path) -> dict[str, tuple[str, int]]:
    """artist_key -> (mbid, found). Empty dict when the cache is absent.

    Read-only: this script never writes the shared cache, so a crash here
    cannot corrupt the one artefact that survived the reset.
    """
    if not cache_path.is_file():
        return {}
    con = sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True)
    try:
        return {
            key: (mbid or "", int(found))
            for key, mbid, found in con.execute(
                "SELECT artist_key, mbid, found FROM mb_artist"
            )
        }
    finally:
        con.close()


# ── planning ──────────────────────────────────────────────────────────────────


def scan(root: Path, cache: dict[str, tuple[str, int]], limit: int | None):
    """Decide for every file. Returns (planned, unresolved artists, tally)."""
    planned: list[dict[str, str]] = []
    unresolved: dict[str, list[Path]] = {}
    tally: Counter = Counter()

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _AUDIO_SUFFIXES:
            continue
        tally["files read"] += 1

        artist = read_artist(path)
        if artist is None:
            tally["unreadable"] += 1
            continue
        if read_identity(path).get("mb_artist_id"):
            tally["already tagged on disk"] += 1
            continue
        if not artist:
            tally["no artist to look up"] += 1
            continue

        hit = cache.get(artist.lower())
        if hit and hit[1] and hit[0]:
            tally["from cache"] += 1
            if limit is None or len(planned) < limit:
                planned.append(
                    {"path": str(path), "artist": artist, "mb_artist_id": hit[0],
                     "source": "cache"}
                )
        elif hit and not hit[1]:
            # An ANSWER, not a gap. MusicBrainz has no such artist.
            tally["cached miss -- AcoustID's job"] += 1
        else:
            tally["needs a network lookup"] += 1
            unresolved.setdefault(artist, []).append(path)

    return planned, unresolved, tally


def resolve_online(unresolved: dict[str, list[Path]]) -> tuple[list[dict[str, str]], Counter]:
    """Ask MusicBrainz for the artists the cache does not know.

    A LookupUnavailable is NOT an answer: the artist is left for another run
    rather than recorded as missing. That distinction is finding #15.
    """
    from musaeus.stages.mb_enrich import LookupUnavailable, _search_artist

    planned: list[dict[str, str]] = []
    tally: Counter = Counter()

    with policy(NetworkPolicy.ALLOWED):
        for i, (artist, paths) in enumerate(sorted(unresolved.items())):
            if i:
                time.sleep(_MB_COURTESY_S)
            try:
                match = _search_artist(artist)
            except LookupUnavailable as exc:
                tally["NO ANSWER -- will retry on a later run"] += 1
                print(f"  no answer for {artist!r}: {exc}", file=sys.stderr)
                continue
            if match is None:
                tally["MB answered: no such artist"] += 1
                continue
            mbid, canonical = match
            tally["found online"] += len(paths)
            for p in paths:
                planned.append(
                    {"path": str(p), "artist": artist, "mb_artist_id": mbid,
                     "mb_name": canonical, "source": "network"}
                )
    return planned, tally


# ── apply / undo ──────────────────────────────────────────────────────────────


def _journal(fh: Any, rec: dict[str, str]) -> None:
    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    fh.flush()
    os.fsync(fh.fileno())


def apply(planned: list[dict[str, str]], journal_path: Path) -> Counter:
    result: Counter = Counter()
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with open(journal_path, "w", encoding="utf-8") as fh:
        for rec in planned:
            _journal(fh, rec)  # durable before the file is touched
            ok, detail = write_identity(
                Path(rec["path"]), {"mb_artist_id": rec["mb_artist_id"]}
            )
            if ok:
                result["written and verified"] += 1
            else:
                result["FAILED"] += 1
                print(f"  FAILED  {rec['path']}\n          {detail}", file=sys.stderr)
    return result


def undo(journal_path: Path) -> Counter:
    """Remove the identity tags this journal recorded writing."""
    from mutagen.mp4 import MP4  # type: ignore[import-untyped]

    from musaeus.identity_tags import IDENTITY_FIELDS, _m4a_key

    key = _m4a_key(IDENTITY_FIELDS["mb_artist_id"])
    result: Counter = Counter()
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        path = Path(rec["path"])
        if not path.exists():
            result["skipped: file is gone"] += 1
            continue
        current = read_identity(path).get("mb_artist_id", "")
        if not current:
            result["already back"] += 1
            continue
        if current != rec["mb_artist_id"]:
            result["REFUSED: a different id is there now"] += 1
            continue
        try:
            audio: Any = MP4(str(path))
            if audio.tags is not None and key in audio.tags:
                del audio.tags[key]
                audio.save()
            result["removed"] += 1
        except Exception as exc:
            result["FAILED"] += 1
            print(f"  FAILED  {path}: {exc}", file=sys.stderr)
    return result


# ── entry point ───────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--root", type=Path, help="library root (default: MUSAEUS_ALAC_LIBRARY)")
    ap.add_argument("--cache", type=Path, help="mb_cache.db (default: from config)")
    ap.add_argument("--apply", action="store_true", help="write; without it this is a dry run")
    ap.add_argument("--network", action="store_true", help="also ask MB for uncached artists")
    ap.add_argument("--limit", type=int, help="stop after this many cache-sourced writes")
    ap.add_argument("--undo", type=Path, metavar="JOURNAL")
    ap.add_argument("--journal", type=Path)
    args = ap.parse_args()

    if args.undo:
        if not args.undo.is_file():
            print(f"No such journal: {args.undo}", file=sys.stderr)
            return 2
        for k, v in undo(args.undo).most_common():
            print(f"  {v:6d}  {k}")
        return 0

    from musaeus.config import MusicConfig

    try:
        cfg = MusicConfig.from_env()
        root = args.root or cfg.alac_library
        cache_path = args.cache or cfg.mb_cache_path
    except Exception as exc:
        print(f"Could not resolve config ({exc}). Pass --root and --cache.", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2

    cache = load_cache(cache_path)
    print(f"Root:  {root}")
    print(f"Cache: {cache_path}  ({len(cache)} artist(s))")
    if not cache:
        print("  WARNING: cache empty or missing -- everything will need the network.")

    planned, unresolved, tally = scan(root, cache, args.limit)
    for k, v in tally.most_common():
        print(f"  {v:6d}  {k}")

    if unresolved:
        print(f"\n  {len(unresolved)} artist(s) not in the cache:")
        for a in sorted(unresolved)[:20]:
            print(f"      {a!r} ({len(unresolved[a])} file(s))")
        if not args.network:
            print("      (pass --network to look these up)")

    if args.network and unresolved:
        if not args.apply:
            print("\n  --network without --apply: skipping lookups in a dry run.")
        else:
            print(f"\nAsking MusicBrainz about {len(unresolved)} artist(s)...")
            found, online_tally = resolve_online(unresolved)
            for k, v in online_tally.most_common():
                print(f"  {v:6d}  {k}")
            planned.extend(found)

    if not planned:
        print("\nNothing to write.")
        return 0

    if not args.apply:
        print(f"\nDRY RUN — nothing written. --apply would write {len(planned)}.")
        return 0

    journal = args.journal or (
        cfg.runs_root
        / "identity_backfill"
        / f"identity_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    )
    print(f"\nWriting {len(planned)} identity tag(s). Journal: {journal}")
    result = apply(planned, journal)
    for k, v in result.most_common():
        print(f"  {v:6d}  {k}")
    print(f"\nTo reverse:  python3 {Path(__file__).name} --undo {journal}")
    return 1 if result["FAILED"] else 0


if __name__ == "__main__":
    sys.exit(main())
