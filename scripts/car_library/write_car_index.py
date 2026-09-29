#!/usr/bin/env python3
"""Write an AAC edition's browsing index (M3U8s in <edition>/Playlists).

The writer lives in musaeus.edition_index; `musaeus edition-build car|iphone`
runs it after every build. This is the by-hand route.

Usage:
    python3 scripts/car_library/write_car_index.py                   # dry run
    python3 scripts/car_library/write_car_index.py --apply
    python3 scripts/car_library/write_car_index.py --edition iphone --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from musaeus.config import MusicConfig  # noqa: E402
from musaeus.edition_build import KINDS  # noqa: E402
from musaeus.edition_index import index_dir_for, write_index  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry run")
    ap.add_argument("--edition", choices=("car", "iphone"), default="car")
    args = ap.parse_args()

    cfg = MusicConfig.from_env()
    # The configured folder (MUSAEUS_CAR_LIBRARY and all): this built it
    # from the vault's Libraries and ignored the override (review of #53).
    edition_root = KINDS[args.edition].root(cfg)
    if not edition_root.is_dir():
        print(f"ERROR: no such edition: {edition_root}")
        return 1

    index_dir = index_dir_for(edition_root)
    print(f"   edition : {edition_root}")
    print(f"   index   : {index_dir}")
    print(f"   mode    : {'APPLY' if args.apply else 'DRY RUN'}\n")

    notes, problems = write_index(cfg, edition_root, apply=args.apply)
    for note in notes:
        print(f"   {note}")

    if not args.apply:
        print("\n   Nothing was written. Re-run with --apply.")
        return 0

    if problems:
        print(f"\n   VERIFY FAILED ({len(problems)}):")
        for p in problems[:20]:
            print(f"     {p}")
        return 1
    n = len(list(index_dir.glob("*.m3u8")))
    print(f"\n   verified: {n} playlist(s), every entry resolves, none absolute")
    return 0


if __name__ == "__main__":
    sys.exit(main())
