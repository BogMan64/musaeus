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
  - Symlinks and anything outside `--root` are refused -- when stripping and
    when undoing.
  - A journal is written before the first file is touched, fsynced to the
    disk before the file it describes, and never overwritten: a second batch
    cannot delete the first batch's record. `--undo` puts the numbers back.
  - `--apply` and `--undo` take the masters lock. A dry run never does.
  - An MP3 is read and written at its own ID3 version, so removing the track
    number cannot drop a frame only v2.3 defines.
  - The batch size defaults to 20 (`--max-strip`). Grey, 2026-11-27: batches
    of 20 until one is clean, then 100.

Usage:
    python3 scripts/strip_track.py                      # dry run, whole ARCHIVE
    python3 scripts/strip_track.py --limit 20           # dry run, 20 files
    python3 scripts/strip_track.py --apply --limit 20
    python3 scripts/strip_track.py --apply             # 20 files; this is the batch
    python3 scripts/strip_track.py --apply --max-strip 100
    python3 scripts/strip_track.py --undo JOURNAL.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_AUDIO_SUFFIXES = (".m4a", ".mp4", ".alac", ".flac", ".mp3")


def _absolute(path: Path) -> Path:
    """The same file, named from / rather than from the working directory.

    A journal line that says "music/song.m4a" means a different file -- or
    none -- when --undo is run from somewhere else (CodeRabbit on #340,
    2026-11-27).
    """
    return Path(os.path.abspath(path))


def _mp3_major_version(path: Path) -> int:
    """The major version in the file's ID3 header: 3 or 4.

    mutagen reads a v2.3 file as v2.4 by default, and dropping frames that
    only v2.3 defines (RVAD among them) is part of that conversion. Saving
    then commits the loss, even though the write only asked to remove the
    track number -- and the journal records the track number alone, so the
    frame could not be restored. Read and write at the file's own version
    instead (CodeRabbit on #340, 2026-11-27; measured with mutagen 1.46.0).
    """
    with open(path, "rb") as fh:
        header = fh.read(5)
    if len(header) < 5 or header[:3] != b"ID3" or header[3] not in (3, 4):
        return 4
    return header[3]


def _mp3(path: Path) -> Any:
    """The file's ID3 tag, read without converting it to another version."""
    from mutagen.id3 import ID3  # type: ignore[import-untyped]

    version = _mp3_major_version(path)
    return ID3(str(path), translate=False, v2_version=version), version


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


def read_track_value(path: Path) -> Any:
    """The complete track tag as the format stores it, or None.

    `read_track` answers "what number is this?" for the operator. This answers
    "what would have to be written back?", and they are not the same for MP4:
    `trkn` holds a position AND a total, [(7, 12)], and the display reader
    returned only the 7. Undo then wrote [(7, 0)] and the total was gone
    (CodeRabbit on #340, second review).

    JSON-safe: a list for MP4, a string for FLAC and MP3 (which keep the total
    in the same tag as text, "7/12").
    """
    suffix = path.suffix.lower()
    try:
        if suffix in (".m4a", ".mp4", ".alac"):
            trkn = (_m4a(path).tags or {}).get("trkn")
            if not trkn or not trkn[0]:
                return None
            pair = trkn[0]
            return [int(v) for v in pair] if len(pair) > 1 else [int(pair[0])]
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


def _display(value: Any) -> str:
    """The value as one readable number, for the journal's "track" field."""
    if isinstance(value, (list, tuple)):
        position = int(value[0])
        total = int(value[1]) if len(value) > 1 and value[1] else 0
        return f"{position}/{total}" if total else str(position)
    return str(value)


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
            audio, version = _mp3(tmp)
            audio.delall("TRCK")
            audio.save(v2_version=version)
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
    """Every audio file under *root*, and what was skipped and why.

    os.walk with followlinks=False, not rglob: rglob descends into a symlinked
    folder, so a link inside the library was a way for a write to land outside
    the tree the operator named (CodeRabbit on #340, second review).
    """
    tally: Counter = Counter()
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for name in sorted(dirnames):
            if (here / name).is_symlink():
                tally["symlinked folder -- not entered"] += 1
        dirnames[:] = sorted(d for d in dirnames if not (here / d).is_symlink())
        for name in sorted(filenames):
            path = here / name
            if path.is_symlink():
                tally["symlink -- refused"] += 1
                continue
            if path.suffix.lower() not in _AUDIO_SUFFIXES:
                continue
            # Before appending, not after: with the check afterwards a limit of
            # 0 still selected one file, and an apply run stripped it
            # (CodeRabbit on #340, 2026-11-27).
            if limit is not None and len(found) >= limit:
                return found, tally
            found.append(path)
    return found, tally


def _sync(fh: Any) -> None:
    """Hand the journal line to the disk, not just to the OS.

    flush() leaves it in the page cache. A power cut could then keep the
    stripped file and lose the record of what it was, which is the one thing
    --undo has (CodeRabbit on #340, 2026-11-27).
    """
    fh.flush()
    os.fsync(fh.fileno())


def journal(fh: Any, path: Path, value: Any, root: Path | None = None) -> None:
    """Durable before the file is touched, so --undo can put it back.

    "track" is the readable number; "value" is the whole tag, because an MP4
    track is a position AND a total and a scalar cannot restore the second.
    "root" is what the run was made against, so undo knows where its own tree
    ends. Older journals have neither, and undo still reads them.
    """
    fh.write(
        json.dumps(
            {
                "path": str(_absolute(path)),
                "track": _display(value),
                "value": value,
                "root": str(root) if root is not None else None,
                "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        + "\n"
    )
    _sync(fh)


def _m4a_track_pair(value: Any) -> tuple[int, int]:
    """(position, total) from a journal value, old or new."""
    if isinstance(value, (list, tuple)):
        position = int(value[0])
        total = int(value[1]) if len(value) > 1 and value[1] else 0
        return position, total
    position, _, total = str(value).partition("/")
    return int(position), int(total) if total.isdigit() else 0


def _track_text(value: Any) -> str:
    """The value as a FLAC or MP3 track tag: "7", or "7/12" when it had a total."""
    return _display(value) if isinstance(value, (list, tuple)) else str(value)


def _path_refusal(path: Path, root: Path | None) -> str | None:
    """Why this record must not be restored, or None when it is safe.

    write_beside replaces the file the path resolves to, so a link in the last
    name OR in a folder between the recorded root and the file makes it write
    somewhere else -- another album, or another library. The root itself is
    not checked: where the library lives is the operator's choice
    (CodeRabbit on #340, second review).
    """
    if path.is_symlink():
        return "symlink -- refused"
    if root is None:
        return None  # a journal from before the root was recorded
    try:
        path.relative_to(root)
    except ValueError:
        return "outside the recorded root -- refused"
    current = path.parent
    while current != root and current != current.parent:
        if current.is_symlink():
            return "symlinked folder -- refused"
        current = current.parent
    return None


def undo(journal_path: Path) -> Counter:
    """Put the recorded track numbers back."""
    from musaeus.safe_save import write_beside

    counts: Counter = Counter()
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        path = Path(rec["path"])
        root = Path(rec["root"]) if rec.get("root") else None
        refusal = _path_refusal(path, root)
        if refusal is not None:
            counts[refusal] += 1
            continue
        if not path.is_file():
            # A direct increment: counts["missing"]["path"] was a TypeError on
            # the zero Counter value, and the first deleted file aborted the
            # whole undo (CodeRabbit on #340).
            counts["missing"] += 1
            continue
        # "value" is the whole tag; "track" alone is a journal from before it
        # was recorded, and still restores.
        value = rec.get("value", rec["track"])

        suffix = path.suffix.lower()  # NOT tmp's: the copy is "<name>.tagging"

        def write(tmp: Path, value: Any = value, suffix: str = suffix) -> bool:
            if suffix in (".m4a", ".mp4", ".alac"):
                audio = _m4a(tmp)
                if audio.tags is None:
                    return False
                audio.tags["trkn"] = [_m4a_track_pair(value)]
                audio.save()
            elif suffix == ".flac":
                from mutagen.flac import FLAC  # type: ignore[import-untyped]

                audio = FLAC(str(tmp))
                audio["tracknumber"] = _track_text(value)
                audio.save()
            elif suffix == ".mp3":
                from mutagen.id3 import TRCK

                audio, version = _mp3(tmp)
                audio.delall("TRCK")
                audio.add(TRCK(encoding=3, text=[_track_text(value)]))
                audio.save(v2_version=version)
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


def _at_least_one(text: str) -> int:
    """An argparse type for a count that only makes sense as 1 or more."""
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, not {value}")
    return value


def _no_config(error: Exception | None, *, doing: str) -> None:
    """Say what to set, not something that cannot help.

    The old message said "Pass --root", which cannot fix a missing vault: the
    masters lock and the journal live under the vault's runs root, and --root
    only chooses which library is scanned (CodeRabbit on #340, 2026-11-27).
    """
    print(f"Could not resolve config ({error}).", file=sys.stderr)
    print(
        f"  Set MUSAEUS_VAULT_ROOT (export it, or run `musaeus setup`);\n"
        f"  {doing} needs the vault for the masters lock and the journal, and --root\n"
        f"  only chooses which library is scanned.",
        file=sys.stderr,
    )


class _masters_held:  # noqa: N801 - reads like a context manager at the call site
    """The exclusive masters lock, or a printed refusal and EXIT_BUSY.

    Undo writes masters too, so it takes the same lock as an apply run: it
    used to return before the lock was taken and could rewrite a master while
    another job held it (CodeRabbit on #340, 2026-11-27).

    The block form, rather than hold_for_process, so a run that applies and
    then undoes is not blocked by its own hold.
    """

    def __init__(self, runs_root: Path) -> None:
        self._runs_root = runs_root
        self._cm: Any = None

    def __enter__(self) -> None:
        from musaeus.masters_lock import EXIT_BUSY, MastersBusy, masters_lock

        self._cm = masters_lock(self._runs_root, exclusive=True, what="scripts/strip_track.py")
        try:
            self._cm.__enter__()
        except MastersBusy as exc:
            print(f"NOT RUN: {exc}", file=sys.stderr)
            raise SystemExit(EXIT_BUSY) from None
        return None

    def __exit__(self, *exc: Any) -> None:
        self._cm.__exit__(*exc)


def _sync_dir(path: Path) -> None:
    """Make a new journal's NAME durable, not only its contents."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return  # a platform that cannot open a directory: nothing to do
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--root", type=Path, help="library root (default: the vault's ARCHIVE)")
    ap.add_argument("--apply", action="store_true", help="write; without this nothing changes")
    ap.add_argument(
        "--limit", type=_at_least_one, help="stop after this many audio files (1 or more)"
    )
    ap.add_argument(
        "--max-strip",
        type=_at_least_one,
        default=20,
        metavar="N",
        help="stop after removing N track numbers (a batch, 20 by default); "
        "the next run carries on where this one stopped",
    )
    ap.add_argument("--journal", type=Path, help="where to record the numbers removed")
    ap.add_argument("--undo", type=Path, metavar="JOURNAL", help="put recorded numbers back")
    args = ap.parse_args()

    from musaeus.config import MusicConfig

    cfg = None
    cfg_error: Exception | None = None
    try:
        cfg = MusicConfig.from_env()
    except Exception as exc:  # ValueError when the vault is not configured
        cfg_error = exc

    if args.undo:
        if not args.undo.is_file():
            print(f"No such journal: {args.undo}", file=sys.stderr)
            return 2
        if cfg is None:
            _no_config(cfg_error, doing="--undo")
            return 2
        with _masters_held(cfg.runs_root):
            counts = undo(args.undo)
        for k, v in counts.most_common():
            print(f"  {v:6d}  {k}")
        # Not 0: a restore that could not be proven is not a success.
        return 1 if counts["failed"] else 0

    root_source = args.root if args.root is not None else (cfg.alac_archive if cfg else None)
    if root_source is None:
        _no_config(cfg_error, doing="reading the archive")
        return 2
    root = _absolute(root_source)
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2

    if args.apply and cfg is None:
        # A dry run needs nothing but --root. A write needs the lock and the
        # journal, and both live in the vault's runs root.
        _no_config(cfg_error, doing="--apply")
        return 2

    if args.apply:
        with _masters_held(cfg.runs_root):
            return _apply(root, args, cfg)

    files, tally = scan(root, args.limit)
    _examine(root, files, tally)
    print("\nDRY RUN -- nothing written. Add --apply to remove them.")
    return 0


def _examine(
    root: Path, files: list[Path], tally: Counter
) -> tuple[list[tuple[Path, Any]], list[tuple[Path, str]]]:
    """Print what was found, and what could not be read at all."""
    print(f"Root: {root}")
    print(f"Audio files: {len(files):,}")
    for k, v in tally.most_common():
        print(f"  {v:6d}  {k}")

    numbered: list[tuple[Path, Any]] = []
    unreadable: list[tuple[Path, str]] = []
    for p in files:
        try:
            t = read_track_value(p)  # the whole tag: undo needs all of it
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
    if numbered:
        for path, value in numbered[:10]:
            print(f"  {_display(value):>4}  {path.relative_to(root)}")
        if len(numbered) > 10:
            print(f"  ... and {len(numbered) - 10:,} more")
    else:
        print("\nNothing to do.")
    return numbered, unreadable


def _apply(root: Path, args: argparse.Namespace, cfg: Any) -> int:
    """One batch: scan, then strip at most --max-strip files. Lock is held."""
    from musaeus.immutability import locked_in, unlock_hint

    # The ARCHIVE is chattr +i on purpose. Refuse the whole run before the
    # first copy is made, rather than stopping on file one of 7,975.
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
    numbered, _ = _examine(root, files, tally)
    if not numbered:
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    journal_path = args.journal or (
        cfg.runs_root
        / "reports"
        / f"strip_track_{stamp}_{uuid.uuid4().hex[:6]}.jsonl"  # no two batches share a name
    )
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # "x", not "w": a journal is never overwritten. With "w" a second batch
        # sharing a --journal path deleted the first batch's recovery records,
        # and those files stayed stripped (CodeRabbit on #340, 2026-11-27).
        fh = open(journal_path, "x", encoding="utf-8")  # noqa: SIM115 -- closed below
    except FileExistsError:
        print(
            f"NOT RUN: {journal_path} already exists.\n"
            "  A journal is the only record of what was removed, so it is never\n"
            "  overwritten. Give this batch a new --journal path, or --undo that one first.",
            file=sys.stderr,
        )
        return 2
    _sync_dir(journal_path.parent)

    changed = 0
    not_removed = 0
    with fh:
        for path, value in numbered:
            if changed >= args.max_strip:
                break
            try:
                journal(fh, path, value, root)  # durable before the file is touched
                if strip_one(path, apply=True):
                    changed += 1
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
    if left:
        print(f"Batch limit reached ({args.max_strip}). Run again to carry on.")
    print(f"Journal: {journal_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
