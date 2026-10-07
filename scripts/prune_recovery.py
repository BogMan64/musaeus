#!/usr/bin/env python3
"""
Retention for RUNS/recovery, which nothing has ever pruned.

Why this is needed now
----------------------
`MutationBoundary` writes a checkpoint per finalize so the run can be rolled
back. Nothing removes them -- there is no prune, cleanup or rmtree anywhere
in the safety layer. Today that costs almost nothing: 9 checkpoints, 17 MB,
because canonicalize is all-PASSTHROUGH and the payload is captured TAGS
rather than audio (468.7 GB -> 5.86 MB, the trick that made library
checkpointing possible at all).

`work/act23` changes that. It puts canonicalize behind the boundary, and
canonicalize rewrites the audio stream -- so its checkpoints must copy
SOURCE AUDIO. A genuine fresh-import batch would retain close to its full
source size. This has to exist before that merges.

The numbers here are placeholders, not a policy
-----------------------------------------------
`KEEP_LAST` and `MIN_AGE_DAYS` below are deliberately conservative defaults
so this is safe to run today. **The actual retention policy is Grey's to
set** -- it is a question about how far back a rollback must remain
possible, which is a judgement about the library, not about the code.
Override per-run with --keep-last / --min-age-days, or by env.

What it refuses to touch, and why
---------------------------------
A checkpoint is the ONLY way to reverse a finalize. So:

  * the newest `KEEP_LAST` are never candidates, whatever their age;
  * nothing younger than `MIN_AGE_DAYS` is a candidate, whatever the count;
  * a directory with no readable `manifest.json` is refused rather than
    deleted -- that shape is either a mid-write checkpoint or something this
    script does not understand, and "delete what you cannot read" is how
    recoverability disappears quietly.

Both gates must pass. They are AND, not OR, so a burst of runs in one day
cannot age out a checkpoint the count would have kept.

Deletion is real and irreversible; --apply is required, and prints each
directory as it goes.

Usage:
    python3 scripts/prune_recovery.py                       # dry run
    python3 scripts/prune_recovery.py --keep-last 10
    python3 scripts/prune_recovery.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

KEEP_LAST = int(os.environ.get("MUSAEUS_RECOVERY_KEEP_LAST", "5"))
MIN_AGE_DAYS = int(os.environ.get("MUSAEUS_RECOVERY_MIN_AGE_DAYS", "14"))


@dataclass(frozen=True)
class Checkpoint:
    path: Path
    created_at: datetime | None
    size_bytes: int
    readable: bool

    @property
    def name(self) -> str:
        return self.path.name


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _created_at(path: Path) -> datetime | None:
    """From the manifest, falling back to mtime. None when unreadable."""
    manifest = path / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return None
    stamp = data.get("created_at")
    if isinstance(stamp, str):
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def scan(recovery_root: Path) -> list[Checkpoint]:
    """Every checkpoint under *recovery_root*, newest first."""
    if not recovery_root.is_dir():
        return []
    found: list[Checkpoint] = []
    for entry in recovery_root.iterdir():
        if not entry.is_dir():
            continue
        created = _created_at(entry)
        found.append(
            Checkpoint(
                path=entry,
                created_at=created,
                size_bytes=_dir_size(entry),
                readable=(entry / "manifest.json").is_file() and created is not None,
            )
        )
    # Unreadable ones sort last; they are never candidates anyway.
    found.sort(key=lambda c: (c.created_at is not None, c.created_at or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return found


def select_for_deletion(
    checkpoints: list[Checkpoint],
    keep_last: int = KEEP_LAST,
    min_age_days: int = MIN_AGE_DAYS,
    now: datetime | None = None,
) -> tuple[list[Checkpoint], dict[str, str]]:
    """(deletable, {name: why it was kept}).

    Both gates must pass for deletion. Returning the reasons rather than just
    the list so a run can say what it protected, not only what it removed.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=min_age_days)

    deletable: list[Checkpoint] = []
    kept: dict[str, str] = {}

    for index, cp in enumerate(checkpoints):
        if not cp.readable:
            kept[cp.name] = "no readable manifest.json -- refusing to delete"
            continue
        if index < keep_last:
            kept[cp.name] = f"among the newest {keep_last}"
            continue
        assert cp.created_at is not None  # readable implies a timestamp
        if cp.created_at > cutoff:
            age = (now - cp.created_at).days
            kept[cp.name] = f"only {age}d old (minimum {min_age_days}d)"
            continue
        deletable.append(cp)

    return deletable, kept


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}B" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--root", type=Path, help="recovery root (default: RUNS/recovery)")
    ap.add_argument("--keep-last", type=int, default=KEEP_LAST)
    ap.add_argument("--min-age-days", type=int, default=MIN_AGE_DAYS)
    ap.add_argument("--apply", action="store_true", help="actually delete")
    args = ap.parse_args()

    root = args.root
    if root is None:
        from musaeus.config import MusicConfig

        try:
            root = MusicConfig.from_env().runs_root / "recovery"
        except Exception as exc:
            print(f"Could not resolve config ({exc}). Pass --root.", file=sys.stderr)
            return 2

    checkpoints = scan(root)
    print(f"Root: {root}")
    print(f"Policy: keep the newest {args.keep_last}, and anything under "
          f"{args.min_age_days} days old\n")
    if not checkpoints:
        print("  No checkpoints.")
        return 0

    total = sum(c.size_bytes for c in checkpoints)
    print(f"  {len(checkpoints)} checkpoint(s), {_human(total)} total\n")

    deletable, kept = select_for_deletion(
        checkpoints, args.keep_last, args.min_age_days
    )

    for cp in checkpoints:
        if cp.name in kept:
            print(f"    KEEP    {cp.name:44s} {_human(cp.size_bytes):>9s}  {kept[cp.name]}")
    for cp in deletable:
        print(f"    DELETE  {cp.name:44s} {_human(cp.size_bytes):>9s}")

    if not deletable:
        print("\n  Nothing is eligible for deletion.")
        return 0

    freed = sum(c.size_bytes for c in deletable)
    if not args.apply:
        print(f"\nDRY RUN — nothing deleted. --apply would remove "
              f"{len(deletable)} checkpoint(s), freeing {_human(freed)}.")
        return 0

    print(f"\nDeleting {len(deletable)} checkpoint(s)...")
    failed = 0
    for cp in deletable:
        try:
            shutil.rmtree(cp.path)
            print(f"    removed {cp.name}")
        except OSError as exc:
            failed += 1
            print(f"    FAILED  {cp.name}: {exc}", file=sys.stderr)
    print(f"\n  {len(deletable) - failed} removed, {failed} failed, "
          f"~{_human(freed)} freed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
