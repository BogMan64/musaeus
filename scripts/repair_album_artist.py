#!/usr/bin/env python3
"""
Bring albumartist into line with artist across a library already on disk.

Why a script and not a stage
----------------------------
`TaggerStage` fixes this on ingest, but it works from `archive` rows, and
after the 2026-08-27 campaign finalised, `musaeus.db` holds 87 quarantined
rows and nothing else. The 10,588 finalised files are reachable only
through the filesystem, so this reads its inputs from the FILES.

The rule lives in exactly one place
-----------------------------------
`musaeus.stages.tagger.albumartist_should_follow`. This script decides
nothing on its own -- if the rule changes, both the ingest path and this
repair change together. Measured on the live library 2026-08-29:

      836  rewritten   (292 article/punctuation convention,
                        544 collaboration credit on an album-less single)
      359  kept        credit on a real album -- there it IS the album's artist
      246  kept        classical composer vs performer
      181  kept        differ ONLY by case, and the ARTIST is the damaged one
      110  kept        unrelated
        3  kept        compilation marker

Safety
------
Only the `aART` atom is touched. No audio stream is read, decoded or
re-encoded, so `audio_hash` -- which hashes ffmpeg's raw PCM output, not
the container -- is unchanged by construction. File size and mtime do
change, which is expected: any manifest keyed on `tagged_identity` will
show drift for these files, correctly.

Every write is verified by reading the value back off disk in a FRESH
handle. `_write_tags` in tagger.py returns True without re-reading; that
is the exact shape that let Forge report 12,279 successful writes while
writing nothing (silent-no-op #2). A writer that reports success without
re-reading is a check that cannot fail.

Reversibility
-------------
The journal record for a file is written and fsynced BEFORE its tag is
touched, so a crash mid-write still leaves the old value recorded. This is
finding #16's lesson -- two facts that must move together, updated in one
place and not the other -- applied deliberately in the safe order.

Usage:
    python3 scripts/repair_album_artist.py                     # dry run, everything
    python3 scripts/repair_album_artist.py --limit 20          # dry run, first 20
    python3 scripts/repair_album_artist.py --apply --limit 20  # write 20, then stop
    python3 scripts/repair_album_artist.py --apply             # write all
    python3 scripts/repair_album_artist.py --undo JOURNAL.jsonl
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

from musaeus.stages.tagger import (  # noqa: E402
    _ENSEMBLE,
    _fold_name,
    albumartist_should_follow,
)

_AUDIO_SUFFIXES = (".m4a", ".mp4", ".alac", ".flac")

# mutagen key for each container. albumartist is a standard atom in MP4 --
# NOT a freeform "----:" one, and not the dotted form that cannot serialise.
_M4A_ARTIST, _M4A_ALBUMARTIST = "\xa9ART", "aART"
_M4A_ALBUM, _M4A_GENRE = "\xa9alb", "\xa9gen"


# ── reading ───────────────────────────────────────────────────────────────────


def read_fields(path: Path) -> dict[str, str] | None:
    """artist / albumartist / album / genre as the file actually carries them.

    None when the file cannot be opened -- an unreadable file is skipped and
    counted, never guessed at.
    """
    suffix = path.suffix.lower()
    try:
        if suffix in (".m4a", ".mp4", ".alac"):
            from mutagen.mp4 import MP4  # type: ignore[import-untyped]

            tags: Any = MP4(str(path)).tags or {}

            def g(key: str) -> str:
                v = tags.get(key)
                return str(v[0]).strip() if v else ""

            return {
                "artist": g(_M4A_ARTIST),
                "albumartist": g(_M4A_ALBUMARTIST),
                "album": g(_M4A_ALBUM),
                "genre": g(_M4A_GENRE),
            }
        if suffix == ".flac":
            from mutagen.flac import FLAC  # type: ignore[import-untyped]

            audio = FLAC(str(path))

            def gf(key: str) -> str:
                v = audio.get(key)
                return str(v[0]).strip() if v else ""

            return {
                "artist": gf("artist"),
                "albumartist": gf("albumartist"),
                "album": gf("album"),
                "genre": gf("genre"),
            }
    except Exception:
        return None
    return None


# ── writing, with the read-back that makes it a real check ────────────────────


def write_albumartist(path: Path, value: str) -> tuple[bool, str]:
    """Set albumartist and prove it landed. Returns (ok, detail)."""
    suffix = path.suffix.lower()
    try:
        if suffix in (".m4a", ".mp4", ".alac"):
            from mutagen.mp4 import MP4  # type: ignore[import-untyped]

            audio: Any = MP4(str(path))
            if audio.tags is None:
                audio.add_tags()
            audio[_M4A_ALBUMARTIST] = [value]
            audio.save()

            check: Any = MP4(str(path)).tags or {}
            got = check.get(_M4A_ALBUMARTIST)
            if not got or str(got[0]) != value:
                return False, "albumartist did not survive the write"
            return True, "verified on disk"

        if suffix == ".flac":
            from mutagen.flac import FLAC  # type: ignore[import-untyped]

            audio = FLAC(str(path))
            audio["albumartist"] = [value]
            audio.save()

            got = FLAC(str(path)).get("albumartist")
            if not got or str(got[0]) != value:
                return False, "albumartist did not survive the write"
            return True, "verified on disk"
    except Exception as exc:
        return False, str(exc)
    return False, f"unsupported container {suffix}"


# ── why a file was left alone (for the report, not for the decision) ──────────


def keep_reason(fields: dict[str, str]) -> str:
    artist, aa = fields["artist"], fields["albumartist"]
    album, genre = fields["album"], fields["genre"]
    if not aa:
        return "no albumartist to repair"
    if not artist:
        return "no artist to copy from"
    if aa == artist:
        return "already agrees"
    if _fold_name(aa) in {"various artists", "various", "va", "soundtrack",
                          "original soundtrack", "ost"}:
        return "compilation marker"
    if genre.strip().lower() == "classical" or _ENSEMBLE.search(aa):
        return "classical composer vs performer"
    if aa.lower() == artist.lower():
        return "differs only by case -- the ARTIST is the damaged field"
    if album:
        return "collaboration credit on a real album"
    return "unrelated -- no rule fits"


# ── journal ───────────────────────────────────────────────────────────────────


def journal_append(fh: Any, record: dict[str, str]) -> None:
    """Write one record and force it to disk before the caller mutates anything."""
    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    fh.flush()
    os.fsync(fh.fileno())


# ── passes ────────────────────────────────────────────────────────────────────


def scan(root: Path, limit: int | None) -> tuple[list[dict[str, str]], Counter]:
    """Decide for every file. Returns (planned changes, tally of everything)."""
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

        if albumartist_should_follow(
            fields["artist"],
            fields["albumartist"],
            album=fields["album"],
            genre=fields["genre"],
        ):
            tally["WOULD REWRITE"] += 1
            if limit is None or len(planned) < limit:
                planned.append(
                    {
                        "path": str(path),
                        "artist": fields["artist"],
                        "old_albumartist": fields["albumartist"],
                        "new_albumartist": fields["artist"],
                        "album": fields["album"],
                        "genre": fields["genre"],
                    }
                )
        else:
            tally[f"kept: {keep_reason(fields)}"] += 1

    return planned, tally


def apply(planned: list[dict[str, str]], journal_path: Path) -> Counter:
    """Write each change, journalling BEFORE the mutation so it stays reversible."""
    result: Counter = Counter()
    journal_path.parent.mkdir(parents=True, exist_ok=True)

    with open(journal_path, "w", encoding="utf-8") as fh:
        for rec in planned:
            journal_append(fh, rec)  # durable before the file is touched
            ok, detail = write_albumartist(Path(rec["path"]), rec["new_albumartist"])
            if ok:
                result["written and verified"] += 1
            else:
                result["FAILED"] += 1
                print(f"  FAILED  {rec['path']}\n          {detail}", file=sys.stderr)
    return result


def undo(journal_path: Path) -> Counter:
    """Put back every old albumartist the journal recorded.

    Refuses any file whose current value is neither what we wrote nor what
    we recorded -- something else changed it, and clobbering that would be
    the same mistake in the other direction.
    """
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

        current = fields["albumartist"]
        if current == rec["old_albumartist"]:
            result["already back"] += 1
            continue
        if current != rec["new_albumartist"]:
            result["REFUSED: changed by something else"] += 1
            print(
                f"  REFUSED {path}\n          expected {rec['new_albumartist']!r}, "
                f"found {current!r}",
                file=sys.stderr,
            )
            continue

        ok, detail = write_albumartist(path, rec["old_albumartist"])
        if ok:
            result["restored"] += 1
        else:
            result["FAILED"] += 1
            print(f"  FAILED  {path}\n          {detail}", file=sys.stderr)
    return result


# ── entry point ───────────────────────────────────────────────────────────────


def default_root() -> Path:
    from musaeus.config import MusicConfig

    return MusicConfig.from_env().alac_library


def default_journal_dir() -> Path:
    from musaeus.config import MusicConfig

    return MusicConfig.from_env().runs_root / "albumartist_repair"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, help="library root (default: MUSAEUS_ALAC_LIBRARY)")
    ap.add_argument("--apply", action="store_true", help="write; without it this is a dry run")
    ap.add_argument("--limit", type=int, help="stop after this many changes")
    ap.add_argument("--undo", type=Path, metavar="JOURNAL", help="reverse a previous --apply")
    ap.add_argument("--journal", type=Path, help="where to write the journal")
    args = ap.parse_args()

    if args.undo:
        if not args.undo.is_file():
            print(f"No such journal: {args.undo}", file=sys.stderr)
            return 2
        print(f"Undoing {args.undo}")
        for k, v in undo(args.undo).most_common():
            print(f"  {v:6d}  {k}")
        return 0

    try:
        root = args.root or default_root()
    except Exception as exc:
        print(f"Could not resolve the library root ({exc}). Pass --root.", file=sys.stderr)
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

    print(f"\nFirst {min(10, len(planned))} of {len(planned)} change(s):")
    for rec in planned[:10]:
        print(f"    {rec['old_albumartist']!r:45s} -> {rec['new_albumartist']!r}")

    if not args.apply:
        print(f"\nDRY RUN — nothing written. Re-run with --apply to write {len(planned)}.")
        return 0

    journal = args.journal or (
        default_journal_dir()
        / f"repair_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    )
    print(f"\nApplying {len(planned)} change(s). Journal: {journal}")
    result = apply(planned, journal)
    for k, v in result.most_common():
        print(f"  {v:6d}  {k}")
    print(f"\nTo reverse:  python3 {Path(__file__).name} --undo {journal}")
    return 1 if result["FAILED"] else 0


if __name__ == "__main__":
    sys.exit(main())
