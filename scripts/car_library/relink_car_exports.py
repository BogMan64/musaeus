#!/usr/bin/env python3
"""Link catalogue rows to car files that already exist but were never matched.

THE PROBLEM

The car build matches its output back to the catalogue on (artist, title) read
from the file's TAGS. That works until the catalogue's artist is corrected and
the file's tag is not -- which has now happened three separate ways:

    DB row                  CAR file tag        why
    Antonio Vivaldi         Itzhak Perlman      classical files under the
                                                COMPOSER; the tag has the
                                                performer
    The Eagles              Eagles, The         the tag predates the
                                                2026-09-16 article migration
    Hall & Oates            Daryl Hall          artist_canon rewrote the row

Every one of those files is on disk and correct. The build reported success,
doctor reported a gap, and re-running the encoder did nothing because its own
resume check could see the published file perfectly well -- only the DB link
was missing. 33 rows, measured 2026-09-16.

WHY TITLE ALONE IS NOT ENOUGH

"Please Come Home for Christmas" by the Eagles matches a Pat Benatar file of
the same name. Linking on title would have pointed an Eagles row at a Benatar
recording and called the edition complete. Duration is the confirming signal:
it is cheap, already recorded, and two different recordings of one song
rarely agree to within a second or two.

A row with more than one surviving candidate is left alone and reported. A
guess is worse than a gap here, because a gap is visible and a wrong link is
not.

    python3 scripts/car_library/relink_car_exports.py            # dry run
    python3 scripts/car_library/relink_car_exports.py --execute
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from musaeus.config import MusicConfig  # noqa: E402
from musaeus.duration import TOLERANCE_SEC  # noqa: E402

#: Quote MARKS, not quoted phrases. brackets.py owns the bracket alphabet;
#: this is the one thing it has no opinion about -- the catalogue writes
#: RV 269 Spring where the tag writes RV 269 "Spring", and the word is the
#: same word. Deleting the quoted phrase instead made the two sides differ by
#: exactly the word they agree on.
_QUOTES = re.compile(r"[\"“”‘’]")


def fold(s: str) -> str:
    """Compare titles without the annotations the two sides spell differently.

    strip_bracketed, not a fourth private bracket regex. The 2026-09-02
    audit found three independent copies of this rule and a fourth written
    the same day, none of which knew about {} -- and the semgrep guard
    failed this file for exactly that until it used the shared one.
    """
    from musaeus.brackets import strip_bracketed

    return re.sub(r"[^a-z0-9]+", "", _QUOTES.sub("", strip_bracketed(s or "")).lower())


def probe_duration(p: Path) -> float | None:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(p)],
            capture_output=True, text=True, timeout=20)
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    cfg = MusicConfig.from_env()
    car = Path(cfg.vault_root) / "Libraries" / "CAR_Library"
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000")

    taken = {r[0] for r in conn.execute(
        "SELECT car_export_path FROM archive WHERE COALESCE(car_export_path,'') <> ''")}

    index: dict[str, list[Path]] = {}
    for p in car.rglob("*.m4a"):
        if str(p) in taken:
            continue                      # already another row's file
        index.setdefault(fold(p.stem.split(" - ", 1)[-1]), []).append(p)

    gaps = conn.execute(
        "SELECT id, artist, title, duration, file_path FROM archive "
        "WHERE status='CATALOGUED' AND COALESCE(car_export_path,'') = ''"
    ).fetchall()

    linked = ambiguous = nomatch = 0
    for row in gaps:
        cands = index.get(fold(row["title"]), [])
        if not cands:
            nomatch += 1
            continue
        want = row["duration"]
        if want:
            cands = [p for p in cands
                     if (d := probe_duration(p)) is not None
                     and abs(d - want) <= max(TOLERANCE_SEC, want * 0.02)]
        if len(cands) != 1:
            ambiguous += 1
            if len(cands) > 1:
                print(f"  AMBIGUOUS id={row['id']} {row['artist']} - {row['title'][:40]}"
                      f"  ({len(cands)} candidates)")
            continue
        p = cands[0]
        print(f"  LINK id={row['id']:<6} {row['artist'][:22]:<24}{row['title'][:32]:<34}"
              f"-> {p.parent.parent.name}/{p.name[:36]}")
        if args.execute:
            conn.execute("UPDATE archive SET car_export_path=? WHERE id=?", (str(p), row["id"]))
        taken.add(str(p))
        linked += 1

    if args.execute:
        conn.commit()
    conn.close()
    print(f"\n{'DONE' if args.execute else 'DRY RUN'}: {linked} linked, "
          f"{ambiguous} left alone as ambiguous, {nomatch} with no candidate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
