#!/usr/bin/env python3
"""Correct the masters' ReplayGain tag and keep their loudness measurements in them.

Grey, 2026-10-06. Two things, one save per master:

  replaygain_track_gain  was written from the -23 LUFS (R128) gain, so players that
                         read ReplayGain (-18 reference) played the masters 5 dB too
                         quietly. Now the master's own R128_TRACK_GAIN + 5 dB. The
                         R128 tag itself is right and is left alone.
  MUSAEUS_LOUDNESS_MEASURED  the edition ledger's measurements of this audio
                         (musaeus/master_measurements.py), so they survive the ledger.

Only CATALOGUED masters; a master already right is skipped, so the run can stop and
resume. Dry run unless --execute. Run only while no musaeus process runs.

    python3 scripts/write_master_loudness_tags.py [--limit N] [--ids FILE] [--execute]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.config import get_config  # noqa: E402
from musaeus.edition_ledger import ledger_path, measurements_of, open_for_reading  # noqa: E402
from musaeus.loudness import R128_APPLE_REFERENCE, R128_REFERENCE  # noqa: E402
from musaeus.master_measurements import KEY, encode  # noqa: E402

R128_KEY = "----:com.apple.iTunes:R128_TRACK_GAIN"
RG_KEY = "----:com.apple.iTunes:replaygain_track_gain"


def _text(tags: Any, key: str) -> str | None:
    raw = tags.get(key) if tags is not None else None
    return bytes(raw[0]).decode("utf-8", "replace").strip() if raw else None


def wanted(tags: Any, lufs: float | None, measured: dict) -> dict[str, bytes]:
    """{tag: bytes} this master should hold that it does not. {} = already right."""
    out: dict[str, bytes] = {}
    r128 = _text(tags, R128_KEY)
    if r128 is not None:
        r128_db = int(r128) / 256.0
    elif lufs is not None:
        r128_db = R128_APPLE_REFERENCE - lufs
        out[R128_KEY] = str(int(round(r128_db * 256))).encode()
    else:
        r128_db = None
    if r128_db is not None:
        rg = f"{r128_db + (R128_REFERENCE - R128_APPLE_REFERENCE):+.2f} dB"
        if _text(tags, RG_KEY) != rg:
            out[RG_KEY] = rg.encode()
    if measured:
        blob = encode(measured)
        raw = tags.get(KEY) if tags is not None else None
        if not raw or bytes(raw[0]) != blob:
            out[KEY] = blob
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", type=Path, help="only these catalogue ids (one per line)")
    args = ap.parse_args()

    from mutagen.mp4 import MP4, MP4FreeForm

    cfg = get_config()
    db = sqlite3.connect(cfg.db_path)
    db.row_factory = sqlite3.Row
    ledger = open_for_reading(ledger_path(cfg))
    root = str(Path(cfg.alac_archive)) + "/"
    rows = db.execute(
        "SELECT id, file_path, audio_hash, lufs FROM archive WHERE status = 'CATALOGUED' ORDER BY id"
    ).fetchall()
    if args.ids:
        keep = {int(x) for x in args.ids.read_text().split()}
        rows = [r for r in rows if r["id"] in keep]
    changed = right = no_loudness = not_master = 0
    for r in rows:
        fp = r["file_path"]
        if not fp.startswith(root) or not fp.lower().endswith(".m4a"):
            not_master += 1
            continue
        audio = MP4(fp)
        todo = wanted(audio.tags, r["lufs"], measurements_of(ledger, r["audio_hash"] or ""))
        if not todo:
            right += 1
            continue
        if _text(audio.tags, R128_KEY) is None and r["lufs"] is None:
            no_loudness += 1
        if args.execute:
            if audio.tags is None:
                audio.add_tags()
            for key, value in todo.items():
                audio.tags[key] = [MP4FreeForm(value)]
            audio.save()
        changed += 1
        if args.limit and changed >= args.limit:
            break
        if changed % 500 == 0:
            print(f"  {changed:,} ...", flush=True)
    verb = "changed" if args.execute else "would change"
    print(f"{verb} {changed:,}; already right {right:,}; no loudness known {no_loudness:,}; "
          f"not a master .m4a {not_master:,}")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
