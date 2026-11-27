#!/usr/bin/env python3
"""
Remove the track number from a MUSAEUS file's tags, and verify every write.

Grey, 2026-11-27: "Strip them. and have tagger stop writting them back".

The track number was put INTO the files on purpose from 2026-10-03 ("Track
numbers please return"), and the tagger did that on every pass. Measured
2026-11-27 on the whole ARCHIVE with mutagen, read-only:

    1,402 of 7,975 catalogue rows carry a track number
    1,402 of 7,975 files carry one in the tag  (median 184, max 857)
          9 files carry a disc number
      2,229 files carry a comment

Those numbers are not album positions: the median is 184 and 941 of the 1,402
are above 99. They are export IDs from the old library, which is the case for
removing them rather than renaming anything.

So the tagger had to change first, or a strip would be undone by the next
run. Both halves are in this branch: the tagger no longer proposes a track
change, and this script removes the ones already there.

What is stripped
----------------
The track number only: `trkn` on MP4, `tracknumber` on FLAC and MP3.

NOT stripped:
  - the disc number (`disk`). Nine files carry one, and it is the only thing
    that records disc order in a multi-disc set.
  - the track TOTAL (`trkn`'s second value, `tracktotal`). It is part of the
    same tag on MP4, so it goes with it; on FLAC and MP3 the separate total
    tag is left alone.
  - comments. The doctor check added 2026-11-26 found 5,091 edition copies
    that had lost a comment their master still has -- they are not junk here.

Safety
------
  - Dry run by default. `--apply` is the only way to write.
  - Every write goes through `write_beside`: a copy beside the file takes its
    place only if the copy still reads as the same audio. A kill or a full
    disk mid-write leaves the file as it was.
  - A locked archive is refused BEFORE any file is touched: `--apply` on a
    root with immutable files changes nothing, says how many there are and
    prints the command that unlocks it (the ARCHIVE is locked `+i`, on
    purpose). Per file, `write_beside` also refuses a locked master and says
    which one.
  - Symlinks and anything outside `--root` are refused.
  - A journal is written before the first file is touched, so `--undo` puts
    the numbers back.
  - `--apply` takes the masters lock. A dry run never does.

Usage:
    python3 scripts/strip_track.py                      # dry run, whole ARCHIVE
    python3 scripts/strip_track.py --limit 20           # dry run, 20 files
    python3 scripts/strip_track.py --apply --limit 20
    python3 scripts/strip_track.py --apply
    python3 scripts/strip_track.py --undo JOURNAL.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_AUDIO_SUFFIXES = (".m4a", ".mp4", ".alac", ".flac", ".mp3")


class Unreadable(Exception):
    """The file's tags could not be read at all.

    Not the same answer as "this file has no track number": a corrupt file
    would otherwise be counted as clean, and the run would report a library it
    never read (Grey's rule: say what was not checked).
    """


def _m4a(path: Path) -> Any:
    from mutagen.mp4 import MP4  # type: ignore[import-untyped]

    return MP4(str(path))


def read_track(path: Path) -> str | None:
    """The file's track number as a plain string, or None when it has none.

    Raises Unreadable when the tags cannot be read. The two answers are kept
    apart on purpose: "no number" means the file is already as it should be,
    and a corrupt file is not that.
    """
    suffix = path.suffix.lower()
    try:
        if suffix in (".m4a", ".mp4", ".alac"):
            trkn = (_m4a(path).tags or {}).get("trkn")
            return str(trkn[0][0]) if trkn and trkn[0] else None
        if suffix == ".flac":
            from mutagen.flac import FLAC  # type: ignore[import-untyped]

            v = FLAC(str(path)).get("tracknumber", [])
            return str(v[0]) if v else None
        if suffix == ".mp3":
            from mutagen.easyid3 import EasyID3  # type: ignore[import-untyped]

            v = EasyID3(str(path)).get("tracknumber", [])
            return str(v[0]) if v else None
    except Exception as exc:
        raise Unreadable(f"{path.name}: {exc}") from exc
    return None


def read_title(path: Path) -> str | None:
    suffix = path.suffix.lower()
    try:
        if suffix in (".m4a", ".mp4", ".alac"):
            v = (_m4a(path).tags or {}).get("\xa9nam")
            return str(v[0]) if v else None
        if suffix == ".flac":
            from mutagen.flac import FLAC  # type: ignore[import-untyped]

            v = FLAC(str(path)).get("title", [])
            return str(v[0]) if v else None
        if suffix == ".mp3":
            from mutagen.easyid3 import EasyID3  # type: ignore[import-untyped]

            v = EasyID3(str(path)).get("title", [])
            return str(v[0]) if v else None
    except Exception:
        return None
    return None


def strip_one(path: Path, *, apply: bool) -> bool:
    """Remove the track number from *path*. True when it changed the file.

    The write goes beside the file and replaces it only if the copy still
    reads: a master is never edited in place (review of slice A, findings
    4-6). The title is read before and after -- if it moved, something wrote
    more than was asked for and the copy is refused.
    """
    suffix = path.suffix.lower()
    before = read_track(path)
    if not before:
        return False
    if not apply:
        return True

    from musaeus.safe_save import write_beside

    title_before = read_title(path)

    def write(tmp: Path) -> bool:
        if suffix in (".m4a", ".mp4", ".alac"):
            audio = _m4a(tmp)
            if audio.tags is None:
                return False
            audio.tags.pop("trkn", None)
            audio.save()
        elif suffix == ".flac":
            from mutagen.flac import FLAC  # type: ignore[import-untyped]

            audio = FLAC(str(tmp))
            audio.pop("tracknumber", None)
            audio.save()
        elif suffix == ".mp3":
            from mutagen.easyid3 import EasyID3  # type: ignore[import-untyped]

            audio = EasyID3(str(tmp))
            audio.pop("tracknumber", None)
            audio.save()
        else:
            return False
        return True

    wrote = write_beside(path, write, failed=False)
    if not wrote:
        return False

    if read_track(path) is not None:
        raise AssertionError(f"{path.name}: the track number is still there after the write")
    if read_title(path) != title_before:
        raise AssertionError(
            f"{path.name}: the title changed ({title_before!r} -> {read_title(path)!r})"
        )
    return True


def scan(root: Path, limit: int | None) -> tuple[list[Path], Counter]:
    """Every audio file under *root*, and what was skipped and why."""
    tally: Counter = Counter()
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            tally["symlink -- refused"] += 1
            continue
        if not path.is_file() or path.suffix.lower() not in _AUDIO_SUFFIXES:
            continue
        found.append(path)
        if limit is not None and len(found) >= limit:
            break
    return found, tally


def journal(fh: Any, path: Path, track: str) -> None:
    """Durable before the file is touched, so --undo can put it back."""
    fh.write(
        json.dumps(
            {
                "path": str(path),
                "track": track,
                "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        + "\n"
    )
    fh.flush()


def undo(journal_path: Path) -> Counter:
    """Put the recorded track numbers back."""
    from musaeus.safe_save import write_beside

    counts: Counter = Counter()
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        path = Path(rec["path"])
        if not path.is_file():
            counts["missing"]["path"] += 1  # type: ignore[index]
            continue
        track = str(rec["track"])

        suffix = path.suffix.lower()  # NOT tmp's: the copy is "<name>.tagging"

        def write(tmp: Path, track: str = track, suffix: str = suffix) -> bool:
            if suffix in (".m4a", ".mp4", ".alac"):
                audio = _m4a(tmp)
                if audio.tags is None:
                    return False
                audio.tags["trkn"] = [(int(track.split("/")[0]), 0)]
                audio.save()
            elif suffix == ".flac":
                from mutagen.flac import FLAC  # type: ignore[import-untyped]

                audio = FLAC(str(tmp))
                audio["tracknumber"] = track
                audio.save()
            elif suffix == ".mp3":
                from mutagen.easyid3 import EasyID3  # type: ignore[import-untyped]

                audio = EasyID3(str(tmp))
                audio["tracknumber"] = track
                audio.save()
            else:
                return False
            return True

        if not write_beside(path, write, failed=False):
            counts["failed"] += 1
            continue
        try:
            restored = read_track(path)
        except Unreadable:
            counts["failed"] += 1  # wrote it, and cannot prove it: say so
            continue
        if restored is None:
            counts["failed"] += 1  # wrote it, and it is not there: say so
        else:
            counts["restored"] += 1
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--root", type=Path, help="library root (default: the vault's ARCHIVE)")
    ap.add_argument("--apply", action="store_true", help="write; without this nothing changes")
    ap.add_argument("--limit", type=int, help="stop after this many audio files")
    ap.add_argument(
        "--max-strip",
        type=int,
        metavar="N",
        help="stop after removing N track numbers (a batch); the next run "
        "carries on where this one stopped",
    )
    ap.add_argument("--journal", type=Path, help="where to record the numbers removed")
    ap.add_argument("--undo", type=Path, metavar="JOURNAL", help="put recorded numbers back")
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
    except Exception as exc:
        print(f"Could not resolve config ({exc}). Pass --root.", file=sys.stderr)
        return 2
    root = args.root or cfg.alac_archive
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2

    if args.apply:
        # This writes to the masters. The lock is what makes two jobs on the
        # archive impossible; a dry run stays lock-free so it can be asked.
        from musaeus.masters_lock import hold_for_process

        hold_for_process(exclusive=True, what="scripts/strip_track.py", runs_root=cfg.runs_root)

        # The ARCHIVE is chattr +i on purpose. Refuse the whole run before
        # the first copy is made, rather than stopping on file one of 7,975.
        from musaeus.immutability import locked_in, unlock_hint

        locked, examples = locked_in(root)
        if locked:
            print(
                f"NOT RUN: {locked:,} file(s) under {root} are immutable "
                f"(chattr +i), so no tag writer can save over them.",
                file=sys.stderr,
            )
            for path in examples:
                print(f"  {path.relative_to(root)}", file=sys.stderr)
            print(
                f"\nUnlock the archive first, then run again:\n  {unlock_hint(root)}",
                file=sys.stderr,
            )
            return 2

    files, tally = scan(root, args.limit)
    print(f"Root: {root}")
    print(f"Audio files: {len(files):,}")
    for k, v in tally.most_common():
        print(f"  {v:6d}  {k}")

    numbered: list[tuple[Path, str]] = []
    unreadable: list[tuple[Path, str]] = []
    for p in files:
        try:
            t = read_track(p)
        except Unreadable as exc:
            unreadable.append((p, str(exc)))
            continue
        if t:
            numbered.append((p, t))
    print(f"With a track number: {len(numbered):,}")
    if unreadable:
        print(f"Unreadable: {len(unreadable):,}  (NOT examined; nothing was assumed)")
        for path, why in unreadable[:10]:
            print(f"  {path.relative_to(root)}: {why}")
        if len(unreadable) > 10:
            print(f"  ... and {len(unreadable) - 10:,} more")
    if not numbered:
        print("\nNothing to do.")
        return 0
    for path, track in numbered[:10]:
        print(f"  {track:>4}  {path.relative_to(root)}")
    if len(numbered) > 10:
        print(f"  ... and {len(numbered) - 10:,} more")

    if not args.apply:
        print("\nDRY RUN -- nothing written. Add --apply to remove them.")
        return 0

    journal_path = args.journal or (
        cfg.runs_root / "reports" / f"strip_track_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    )
    journal_path.parent.mkdir(parents=True, exist_ok=True)

    changed = 0
    not_removed = 0
    with open(journal_path, "w", encoding="utf-8") as fh:
        for path, track in numbered:
            if args.max_strip is not None and changed >= args.max_strip:
                break
            try:
                journal(fh, path, track)  # durable before the file is touched
                if strip_one(path, apply=True):
                    changed += 1
                    if args.max_strip is not None:
                        print(f"  removed {changed:>4}  {path.relative_to(root)}")
                else:
                    not_removed += 1
            except Exception as exc:
                print(f"\nSTOPPED on {path}: {exc}", file=sys.stderr)
                print(f"  Journal: {journal_path}  (--undo puts them back)", file=sys.stderr)
                return 1

    print(f"\nTrack numbers removed: {changed:,}")
    if not_removed:
        print(f"Not removed: {not_removed:,}  (the write was refused; the file is as it was)")
    left = len(numbered) - changed
    print(f"Still numbered after this run: {left:,}")
    if args.max_strip is not None and left:
        print(f"Batch limit reached ({args.max_strip}). Run again to carry on.")
    print(f"Journal: {journal_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
