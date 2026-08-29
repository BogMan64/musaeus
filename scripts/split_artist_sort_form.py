#!/usr/bin/env python3
"""
Move the article form out of `artist` and into the sort tag, on disk.

MUSAEUS inherited ORPHEUS's convention of storing the article as a suffix --
"Stooges, The" -- so a folder listing sorts under S. That is a real
requirement. The mistake was putting that string in the `artist` TAG, which
is the field every external service reads.

Measured on the live cache 2026-08-29:
    376 of 839 cached MusicBrainz misses were in `X, The` form   (45%)
      0 of 2,158 cached hits were                                (none, ever)

Not one article-suffix lookup had ever succeeded. `mb_enrich` now flips the
form before asking, which fixes lookups. This fixes it at rest:

    artist (\xa9ART)   "The Stooges"    natural -- MusicBrainz, Plex, players
    soar            "Stooges, The"   sort -- what the atom is FOR
    aART            "The Stooges"    albumartist follows artist
    soaa            "Stooges, The"
    folder          "Stooges, The"   UNCHANGED

Nothing moves on disk
---------------------
`OrganizeStage` derives its paths from `sort_form(artist)` rather than from
the tag, so the layout is identical before and after this runs. That is
deliberate and it is what makes this safe: 370 folders keep their names, and
no `file_path` anywhere goes stale.

Scope
-----
Only files whose artist actually carries an article -- 1,722 of 10,588
measured 2026-08-29. `has_article` decides, which asks whether the two
transforms disagree rather than pattern-matching, so PROTECTED_ARTIST_NAMES
("De La Soul", "Los Lobos") are excluded for free.

albumartist is rewritten only where it already matched the artist, so the
agreement established by the 08-29 albumartist repair (9,528 files) holds.

Usage:
    python3 scripts/split_artist_sort_form.py                    # dry run
    python3 scripts/split_artist_sort_form.py --apply --limit 20
    python3 scripts/split_artist_sort_form.py --apply
    python3 scripts/split_artist_sort_form.py --undo JOURNAL.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.artist_form import has_article, natural_form, sort_form  # noqa: E402

_AUDIO_SUFFIXES = (".m4a", ".mp4", ".alac")
_ATOMS = {
    "artist": "\xa9ART",
    "albumartist": "aART",
    "sort_artist": "soar",
    "sort_albumartist": "soaa",
}


def read_fields(path: Path) -> dict[str, str] | None:
    try:
        from mutagen.mp4 import MP4  # type: ignore[import-untyped]

        tags: Any = MP4(str(path)).tags or {}
    except Exception:
        return None

    def g(key: str) -> str:
        v = tags.get(key)
        return str(v[0]).strip() if v else ""

    return {name: g(atom) for name, atom in _ATOMS.items()}


def write_fields(path: Path, values: dict[str, str]) -> tuple[bool, str]:
    """Write and prove it, reading back in a fresh handle."""
    try:
        from mutagen.mp4 import MP4  # type: ignore[import-untyped]

        audio: Any = MP4(str(path))
        if audio.tags is None:
            audio.add_tags()
        for name, val in values.items():
            audio[_ATOMS[name]] = [val]
        audio.save()

        check: Any = MP4(str(path)).tags or {}
        for name, val in values.items():
            got = check.get(_ATOMS[name])
            if not got or str(got[0]) != val:
                return False, f"{name} did not survive the write"
        return True, "verified on disk"
    except Exception as exc:
        return False, str(exc)


def plan_for(fields: dict[str, str]) -> dict[str, str]:
    """The values to write for one file, or {} when nothing is needed."""
    artist = fields["artist"]
    if not artist or not has_article(artist):
        return {}

    natural, sort = natural_form(artist), sort_form(artist)
    values: dict[str, str] = {}
    if fields["artist"] != natural:
        values["artist"] = natural
    if fields["sort_artist"] != sort:
        values["sort_artist"] = sort

    # albumartist only where it already agreed -- preserving the agreement
    # the 08-29 repair established, and never inventing one.
    aa = fields["albumartist"]
    if aa and aa in (artist, natural, sort):
        if aa != natural:
            values["albumartist"] = natural
        if fields["sort_albumartist"] != sort:
            values["sort_albumartist"] = sort

    return values


def scan(root: Path, limit: int | None):
    planned: list[dict[str, Any]] = []
    tally: Counter = Counter()

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _AUDIO_SUFFIXES:
            continue
        tally["files read"] += 1

        fields = read_fields(path)
        if fields is None:
            tally["unreadable"] += 1
            continue
        if not fields["artist"]:
            tally["no artist"] += 1
            continue
        if not has_article(fields["artist"]):
            tally["no article -- nothing to split"] += 1
            continue

        values = plan_for(fields)
        if not values:
            tally["already split"] += 1
            continue

        tally["WOULD SPLIT"] += 1
        if limit is not None and len(planned) >= limit:
            continue
        planned.append({"path": str(path), "before": fields, "after": values})

    return planned, tally


def _journal(fh: Any, rec: dict[str, Any]) -> None:
    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    fh.flush()
    os.fsync(fh.fileno())


def apply(planned: list[dict[str, Any]], journal_path: Path) -> Counter:
    result: Counter = Counter()
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with open(journal_path, "w", encoding="utf-8") as fh:
        for rec in planned:
            _journal(fh, rec)  # durable before the file is touched
            ok, detail = write_fields(Path(rec["path"]), rec["after"])
            if ok:
                result["written and verified"] += 1
            else:
                result["FAILED"] += 1
                print(f"  FAILED  {rec['path']}\n          {detail}", file=sys.stderr)
    return result


def undo(journal_path: Path) -> Counter:
    """Restore every field this journal recorded changing."""
    result: Counter = Counter()
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        path = Path(rec["path"])
        if not path.exists():
            result["skipped: file is gone"] += 1
            continue
        current = read_fields(path)
        if current is None:
            result["skipped: unreadable"] += 1
            continue

        changed = set(rec["after"])
        if all(current[f] == rec["before"][f] for f in changed):
            result["already back"] += 1
            continue
        if any(current[f] != rec["after"][f] for f in changed):
            result["REFUSED: changed by something else"] += 1
            continue

        ok, detail = write_fields(path, {f: rec["before"][f] for f in changed})
        if ok:
            result["restored"] += 1
        else:
            result["FAILED"] += 1
            print(f"  FAILED  {path}: {detail}", file=sys.stderr)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--root", type=Path)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int)
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
    except Exception as exc:
        print(f"Could not resolve config ({exc}). Pass --root.", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2

    print(f"Root: {root}")
    planned, tally = scan(root, args.limit)
    for k, v in tally.most_common():
        print(f"  {v:6d}  {k}")

    if not planned:
        print("\nNothing to do.")
        return 0

    print(f"\nFirst {min(8, len(planned))} of {len(planned)}:")
    for rec in planned[:8]:
        a = rec["after"]
        print(f"    artist {rec['before']['artist']!r} -> {a.get('artist', '(kept)')!r}"
              f"   soar -> {a.get('sort_artist', '(kept)')!r}")

    if not args.apply:
        print(f"\nDRY RUN — nothing written. --apply would split {len(planned)} file(s).")
        print("Folders do NOT change: organize derives paths from sort_form.")
        return 0

    journal = args.journal or (
        cfg.runs_root
        / "artist_sort_split"
        / f"split_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    )
    print(f"\nSplitting {len(planned)} file(s). Journal: {journal}")
    result = apply(planned, journal)
    for k, v in result.most_common():
        print(f"  {v:6d}  {k}")
    print(f"\nTo reverse:  python3 {Path(__file__).name} --undo {journal}")
    return 1 if result["FAILED"] else 0


if __name__ == "__main__":
    sys.exit(main())
