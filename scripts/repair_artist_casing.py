#!/usr/bin/env python3
"""
Restore artist spellings that `_smart_title()` flattened, using MusicBrainz.

The damage
----------
Title-casing an artist name destroys acronyms and stylized names, and the
result is what the library files under. Measured 2026-08-29 on disk:

    'Tlc'            should be 'TLC'
    'Paul Mccartney' should be 'Paul McCartney'
    'N.w.a'          should be 'N.W.A'
    'Sza'            should be 'SZA'
    'K.d. Lang'      should be 'k.d. lang'

This was found from the other side: a repair asked to make albumartist
follow artist, which on 181 files would have copied the damage over the
one field still holding the correct spelling. albumartist was right and
artist was wrong.

Why MusicBrainz and not a hand-kept list
----------------------------------------
`normalize.PROTECTED_ARTIST_NAMES` exists for the article-splitting bug and
has to be maintained by hand -- it cannot know about an artist nobody has
hit yet. MusicBrainz already publishes the canonical spelling, `mb_cache.db`
holds 2,158 of them, and identity is now on the files as of the same day.
So the authority is external and grows on its own.

Three filters, each measured
----------------------------
1. **Same name only.** Accept `mb_name` only when it folds onto the current
   artist ignoring case and punctuation. A different name is a different
   artist, not a spelling, and is never adopted.

2. **Our punctuation, their casing.** MusicBrainz publishes typographic
   Unicode -- 'Olivia Newton‐John' uses U+2010, 'Bachman–Turner Overdrive'
   an en dash, 'Guns N’ Roses' a curly apostrophe. `sanitize_path_component`
   flattens all of those to ASCII on the way to disk, so adopting them
   verbatim would put the tag and the path permanently out of step. Their
   punctuation is mapped back to ours before comparing; 128 files across 47
   artists differ ONLY in typography and are left alone entirely.

3. **Nothing a filesystem forbids.** MusicBrainz offers '*NSYNC' and
   'Eddie "Cleanhead" Vinson'; `*` and `"` are in `_FORBIDDEN_CHARS` and
   would be rewritten to `-` in any path built from them. Rejected.

What survives all three: **277 files, 126 artists.**

albumartist follows
-------------------
Where albumartist currently equals the old artist, it is corrected too --
otherwise this would break the agreement the 08-29 albumartist repair just
established on 9,403 files.

This does NOT rename folders. A file stays where it is; the folder still
reads `Tlc/` until an organize pass moves it, and `OrganizeStage` is not in
`DEFAULT_PIPELINE`. Tags first, on purpose: a tag write is reversible from
the journal, a directory rename across 126 folders is not.

Usage:
    python3 scripts/repair_artist_casing.py                    # dry run
    python3 scripts/repair_artist_casing.py --apply --limit 20
    python3 scripts/repair_artist_casing.py --apply
    python3 scripts/repair_artist_casing.py --undo JOURNAL.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.stages.organize import _FORBIDDEN_CHARS  # noqa: E402

_AUDIO_SUFFIXES = (".m4a", ".mp4", ".alac", ".flac")
_M4A_ARTIST, _M4A_ALBUMARTIST = "\xa9ART", "aART"

# Exactly the flattening sanitize_path_component performs, so a name that
# survives this comparison is one the filesystem will keep verbatim.
_TO_ASCII = {
    0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201B: "'",
    0x201C: '"', 0x201D: '"',
    0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2013: "-", 0x2014: "-",
    0x2015: "-", 0x2212: "-",
    0x00A0: " ", 0x2044: "/",
}
_FORBIDDEN_RE = re.compile(f"[{re.escape(_FORBIDDEN_CHARS)}]")


def ascii_punctuation(name: str) -> str:
    """MusicBrainz typography mapped to the ASCII this library stores."""
    return unicodedata.normalize("NFC", name).translate(_TO_ASCII)


def fold(name: str) -> str:
    """Letters and digits only -- 'the same name, spelled differently'."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]", "", s)


def canonical_spelling(artist: str, mb_name: str | None) -> str | None:
    """The spelling to adopt, or None to leave the file alone.

    Returns None for every case it is not sure about. This renames the field
    the whole library is filed under.
    """
    if not artist or not mb_name or mb_name == artist:
        return None
    if fold(mb_name) != fold(artist):
        return None                       # a different artist, not a spelling
    candidate = ascii_punctuation(mb_name)
    if candidate == artist:
        return None                       # differs only in typography
    if _FORBIDDEN_RE.search(candidate):
        return None                       # a path built from this would differ
    if not candidate.strip():
        return None
    return candidate


# ── file access ───────────────────────────────────────────────────────────────


def read_fields(path: Path) -> dict[str, str] | None:
    suffix = path.suffix.lower()
    try:
        if suffix in (".m4a", ".mp4", ".alac"):
            from mutagen.mp4 import MP4  # type: ignore[import-untyped]

            tags: Any = MP4(str(path)).tags or {}

            def g(key: str) -> str:
                v = tags.get(key)
                return str(v[0]).strip() if v else ""

            return {"artist": g(_M4A_ARTIST), "albumartist": g(_M4A_ALBUMARTIST)}
        if suffix == ".flac":
            from mutagen.flac import FLAC  # type: ignore[import-untyped]

            audio = FLAC(str(path))

            def gf(key: str) -> str:
                v = audio.get(key)
                return str(v[0]).strip() if v else ""

            return {"artist": gf("artist"), "albumartist": gf("albumartist")}
    except Exception:
        return None
    return None


def write_fields(path: Path, values: dict[str, str]) -> tuple[bool, str]:
    """Write and prove it landed, reading back in a fresh handle."""
    suffix = path.suffix.lower()
    try:
        if suffix in (".m4a", ".mp4", ".alac"):
            from mutagen.mp4 import MP4  # type: ignore[import-untyped]

            keys = {"artist": _M4A_ARTIST, "albumartist": _M4A_ALBUMARTIST}
            audio: Any = MP4(str(path))
            if audio.tags is None:
                audio.add_tags()
            for field, val in values.items():
                audio[keys[field]] = [val]
            audio.save()

            check: Any = MP4(str(path)).tags or {}
            for field, val in values.items():
                got = check.get(keys[field])
                if not got or str(got[0]) != val:
                    return False, f"{field} did not survive the write"
            return True, "verified on disk"

        if suffix == ".flac":
            from mutagen.flac import FLAC  # type: ignore[import-untyped]

            audio = FLAC(str(path))
            for field, val in values.items():
                audio[field] = [val]
            audio.save()

            check = FLAC(str(path))
            for field, val in values.items():
                got = check.get(field)
                if not got or str(got[0]) != val:
                    return False, f"{field} did not survive the write"
            return True, "verified on disk"
    except Exception as exc:
        return False, str(exc)
    return False, f"unsupported container {suffix}"


def load_mb_names(cache_path: Path) -> dict[str, str]:
    """artist_key -> canonical MusicBrainz name, for found artists only."""
    if not cache_path.is_file():
        return {}
    con = sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True)
    try:
        return {
            key: name
            for key, name, found in con.execute(
                "SELECT artist_key, mb_name, found FROM mb_artist"
            )
            if found and name
        }
    finally:
        con.close()


# ── passes ────────────────────────────────────────────────────────────────────


def scan(root: Path, mb_names: dict[str, str], limit: int | None):
    planned: list[dict[str, str]] = []
    tally: Counter = Counter()

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _AUDIO_SUFFIXES:
            continue
        tally["files read"] += 1

        fields = read_fields(path)
        if fields is None:
            tally["unreadable"] += 1
            continue
        artist = fields["artist"]
        if not artist:
            tally["no artist"] += 1
            continue

        mb_name = mb_names.get(artist.lower())
        if not mb_name:
            tally["no cached MusicBrainz name"] += 1
            continue

        corrected = canonical_spelling(artist, mb_name)
        if corrected is None:
            tally["left alone"] += 1
            continue

        tally["WOULD REPAIR"] += 1
        if limit is not None and len(planned) >= limit:
            continue

        rec = {
            "path": str(path),
            "old_artist": artist,
            "new_artist": corrected,
            "mb_name": mb_name,
        }
        # Keep the agreement the albumartist repair established.
        if fields["albumartist"] == artist:
            rec["old_albumartist"] = artist
            rec["new_albumartist"] = corrected
        planned.append(rec)

    return planned, tally


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
            values = {"artist": rec["new_artist"]}
            if "new_albumartist" in rec:
                values["albumartist"] = rec["new_albumartist"]
            ok, detail = write_fields(Path(rec["path"]), values)
            if ok:
                result["written and verified"] += 1
            else:
                result["FAILED"] += 1
                print(f"  FAILED  {rec['path']}\n          {detail}", file=sys.stderr)
    return result


def undo(journal_path: Path) -> Counter:
    result: Counter = Counter()
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        path = Path(rec["path"])
        if not path.exists():
            result["skipped: file is gone"] += 1
            continue
        fields = read_fields(path)
        if fields is None:
            result["skipped: unreadable"] += 1
            continue
        if fields["artist"] == rec["old_artist"]:
            result["already back"] += 1
            continue
        if fields["artist"] != rec["new_artist"]:
            result["REFUSED: changed by something else"] += 1
            print(
                f"  REFUSED {path}: expected {rec['new_artist']!r}, "
                f"found {fields['artist']!r}",
                file=sys.stderr,
            )
            continue
        values = {"artist": rec["old_artist"]}
        if "old_albumartist" in rec:
            values["albumartist"] = rec["old_albumartist"]
        ok, detail = write_fields(path, values)
        if ok:
            result["restored"] += 1
        else:
            result["FAILED"] += 1
            print(f"  FAILED  {path}: {detail}", file=sys.stderr)
    return result


# ── entry point ───────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--root", type=Path)
    ap.add_argument("--cache", type=Path)
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
        cache_path = args.cache or cfg.mb_cache_path
    except Exception as exc:
        print(f"Could not resolve config ({exc}). Pass --root and --cache.", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2

    mb_names = load_mb_names(cache_path)
    print(f"Root:  {root}")
    print(f"Cache: {cache_path}  ({len(mb_names)} canonical name(s))")

    planned, tally = scan(root, mb_names, args.limit)
    for k, v in tally.most_common():
        print(f"  {v:6d}  {k}")

    if not planned:
        print("\nNothing to do.")
        return 0

    seen: dict[tuple[str, str], int] = {}
    for rec in planned:
        seen[(rec["old_artist"], rec["new_artist"])] = (
            seen.get((rec["old_artist"], rec["new_artist"]), 0) + 1
        )
    print(f"\n{len(seen)} distinct artist(s):")
    for (old, new), n in sorted(seen.items(), key=lambda kv: -kv[1])[:25]:
        print(f"    {n:4d}  {old!r:30s} -> {new!r}")

    if not args.apply:
        print(f"\nDRY RUN — nothing written. --apply would repair {len(planned)} file(s).")
        return 0

    journal = args.journal or (
        cfg.runs_root
        / "artist_casing"
        / f"casing_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    )
    print(f"\nRepairing {len(planned)} file(s). Journal: {journal}")
    result = apply(planned, journal)
    for k, v in result.most_common():
        print(f"  {v:6d}  {k}")
    print(f"\nTo reverse:  python3 {Path(__file__).name} --undo {journal}")
    return 1 if result["FAILED"] else 0


if __name__ == "__main__":
    sys.exit(main())
