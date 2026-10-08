#!/usr/bin/env python3
"""Back up the masters to NUC8TB as a dated, checked copy; keep the newest 2.

Grey, 2026-10-07. See musaeus/music_backup.py. Run monthly by
scripts/music_backup_monthly.sh (systemd user timer musaeus-music-backup.timer).

    python3 scripts/music_backup.py                 # what it would do
    python3 scripts/music_backup.py --execute       # do it
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.config import get_config  # noqa: E402
from musaeus.music_backup import KEEP, PREFIX, dated_copies, run  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--root", type=Path, default=Path("/mnt/NUC8TB_BACKUP"))
    ap.add_argument("--keep", type=int, default=KEEP)
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    vault = Path(get_config().vault_root)
    if not args.root.is_dir() or not any(args.root.iterdir()):
        print(f"NOT RUN: {args.root} is not mounted")
        return 2
    copies = dated_copies(args.root)
    print(f"masters: {vault / 'Libraries' / 'ALAC-Archival'}")
    print(
        f"copies on {args.root}: {', '.join(c.name for c in copies) or 'none'}; free {shutil.disk_usage(args.root).free // 10**9} GB"
    )
    if not args.execute:
        print(f"DRY RUN: would make {PREFIX}<today> (linked to {copies[0].name if copies else 'nothing'}), "
              f"check it, then keep the newest {args.keep}")  # fmt: skip
        return 0
    report = run(vault, args.root, args.keep)
    if report.problems:
        print(
            f"BACKUP NOT CONFIRMED -- nothing removed. {report.dest.name}: "
            + "; ".join(report.problems)
        )
        return 1
    if report.unverified:
        print(
            "NOTE: copies left unverified by earlier runs (not counted, not removed): "
            + ", ".join(p.name for p in report.unverified)
        )
    removed = ", ".join(p.name for p in report.removed) or "none"
    print(
        f"BACKUP OK: {report.dest.name}, {report.files:,} files, checked; removed older: {removed}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
