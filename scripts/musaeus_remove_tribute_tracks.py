#!/usr/bin/env python3
"""
One-off, real-data removal of tribute-band/cover recordings from the live
ALAC-Library, per Grey's 2026-08-12 request: these 28 archive rows (14
unique songs) are cover versions credited to a tribute act rather than the
genuine performing artist. They get physically moved out of ALAC-Library
into TRIBUTE_REMOVED_FOR_REVIEW/<date>/ (never deleted -- same reversible
convention as DupeResolver's DUPES_MOVED_FOR_REVIEW), archive.status is set
to 'TRIBUTE_REVIEW' (a new, distinct status -- not CATALOGUED, not
DUPE_REVIEW), an event is logged per move, and one row per unique song is
appended to TuneMyMusic_Tribute_Replacements.csv so Grey can find/buy the
genuine recording later.

Two rows (Neil Young Tribute Band "Cinnamon Girl" id 11098, Piano Tribute
Players "Seven Nation Army" id 11547) were already relocated into
DUPES_MOVED_FOR_REVIEW by the DupeResolver EXACT-cluster fix earlier today
-- they are moved again from there into TRIBUTE_REMOVED_FOR_REVIEW so their
status/location correctly reflects "tribute recording pending replacement"
rather than "flagged as a duplicate".

Deliberately NOT a generic reusable tool: this is a hardcoded, one-time
list of specific rowids identified by hand (Tribute Stars' two tracks have
no source-artist tag at all -- Bob Dylan was identified by song knowledge,
per Grey's explicit confirmation), not something to re-run against future
data automatically.

Usage:
    python3 scripts/musaeus_remove_tribute_tracks.py            # dry run (default)
    python3 scripts/musaeus_remove_tribute_tracks.py --execute  # actually move files + write DB
"""

import argparse
import csv
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/mnt/FORGE2TB/Projects/MUSAEUS")
from musaeus.stages.organize import build_track_filename, sanitize_path_component, unique_path

DB_PATH = "/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/musaeus.db"
ALAC_LIBRARY = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/ALAC-Library")
REVIEW_ROOT = ALAC_LIBRARY / "TRIBUTE_REMOVED_FOR_REVIEW"
TUNEMYMUSIC_CSV = ALAC_LIBRARY / "TuneMyMusic_Tribute_Replacements.csv"
BATCH_DATE = "2026-08-12"

# rowid -> (real_artist, clean_title, tribute_act_tag)
ROWS = {
    # Neil Young Tribute Band, The -> Neil Young
    11097: ("Neil Young", "After the Gold Rush", "Neil Young Tribute Band, The"),
    12877: ("Neil Young", "After the Gold Rush", "Neil Young Tribute Band, The"),
    11098: ("Neil Young", "Cinnamon Girl", "Neil Young Tribute Band, The"),
    12876: ("Neil Young", "Cinnamon Girl", "Neil Young Tribute Band, The"),
    11099: ("Neil Young", "Like A Hurricane", "Neil Young Tribute Band, The"),
    12878: ("Neil Young", "Like A Hurricane", "Neil Young Tribute Band, The"),
    11100: ("Neil Young", "Old Man", "Neil Young Tribute Band, The"),
    12879: ("Neil Young", "Old Man", "Neil Young Tribute Band, The"),
    11101: ("Neil Young", "Southern Man", "Neil Young Tribute Band, The"),
    12880: ("Neil Young", "Southern Man", "Neil Young Tribute Band, The"),
    # Various Artists - The Eagles Tribute -> Eagles
    15916: ("Eagles", "Hotel California", "Various Artists - The Eagles Tribute"),
    16838: ("Eagles", "Hotel California", "Various Artists - The Eagles Tribute"),
    15917: ("Eagles", "Life In The Fast Lane", "Various Artists - The Eagles Tribute"),
    16839: ("Eagles", "Life In The Fast Lane", "Various Artists - The Eagles Tribute"),
    15918: ("Eagles", "Lyin' Eyes", "Various Artists - The Eagles Tribute"),
    16840: ("Eagles", "Lyin' Eyes", "Various Artists - The Eagles Tribute"),
    15919: ("Eagles", "One Of These Nights", "Various Artists - The Eagles Tribute"),
    16841: ("Eagles", "One Of These Nights", "Various Artists - The Eagles Tribute"),
    15920: ("Eagles", "Take It To The Limit", "Various Artists - The Eagles Tribute"),
    16842: ("Eagles", "Take It To The Limit", "Various Artists - The Eagles Tribute"),
    # Tribute Stars -> Bob Dylan (no source-artist tag in file; identified by song)
    15740: ("Bob Dylan", "All Along The Watchtower", "Tribute Stars"),
    16717: ("Bob Dylan", "All Along The Watchtower", "Tribute Stars"),
    15741: ("Bob Dylan", "Knockin' On Heaven's Door", "Tribute Stars"),
    16718: ("Bob Dylan", "Knockin' On Heaven's Door", "Tribute Stars"),
    # Piano Tribute Players -> The White Stripes
    11547: ("The White Stripes", "Seven Nation Army", "Piano Tribute Players"),
    13453: ("The White Stripes", "Seven Nation Army", "Piano Tribute Players"),
    # Garth Brooks Tribute -> Garth Brooks
    6285: ("Garth Brooks", "Friends in Low Places", "Garth Brooks Tribute"),
    6286: ("Garth Brooks", "Friends in Low Places", "Garth Brooks Tribute"),
}


def make_run_id() -> str:
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run_{stamp}_{uuid.uuid4().hex[:6]}"


def target_path(tribute_act: str, album: str, title: str, source: Path) -> Path:
    filename = build_track_filename(tribute_act, title, source.suffix)
    dest_dir = REVIEW_ROOT / BATCH_DATE / sanitize_path_component(tribute_act) / sanitize_path_component(album or "Unsorted")
    return unique_path(dest_dir / filename)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="Actually move files and write DB changes (default: dry run)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    run_id = make_run_id()

    ids = list(ROWS.keys())
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, artist, album, title, file_path, status FROM archive WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    found = {r["id"]: r for r in rows}

    missing = set(ids) - set(found)
    if missing:
        print(f"WARNING: {len(missing)} expected rowid(s) not found in archive: {sorted(missing)}")

    print(f"{'EXECUTING' if args.execute else 'DRY RUN'} -- {len(found)} row(s), run_id={run_id}\n")

    moved_for_csv: dict[tuple[str, str], dict] = {}  # (real_artist, clean_title) -> info
    errors = []
    moved_count = 0

    for rowid, row in sorted(found.items()):
        real_artist, clean_title, tribute_act = ROWS[rowid]
        source = Path(row["file_path"])
        if not source.exists():
            errors.append(f"id={rowid}: file missing on disk: {source}")
            print(f"  [ERROR] id={rowid} missing on disk: {source}")
            continue

        dest = target_path(tribute_act, row["album"], row["title"], source)
        print(f"  id={rowid:<6} [{row['status']:<11}] {source.name}")
        print(f"           -> {dest}")
        print(f"           real artist: {real_artist}")

        key = (real_artist, clean_title)
        moved_for_csv.setdefault(key, {
            "artist": real_artist,
            "title": clean_title,
            "tribute_act": tribute_act,
            "example_tribute_title": row["title"],
            "example_original_path": str(source),
        })

        if args.execute:
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(dest))
            except OSError as exc:
                errors.append(f"id={rowid}: move failed: {exc}")
                print(f"           [ERROR] move failed: {exc}")
                continue

            conn.execute(
                "UPDATE archive SET status = 'TRIBUTE_REVIEW', file_path = ? WHERE rowid = ?",
                (str(dest), rowid),
            )
            conn.execute(
                """
                INSERT INTO events (run_id, event_type, file_path, old_value, new_value, stage, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    "TRIBUTE_MOVED_FOR_REVIEW",
                    str(dest),
                    str(source),
                    str(dest),
                    "tribute-removal-manual",
                    f"tribute_act={tribute_act} real_artist={real_artist} title={clean_title}",
                ),
            )
        moved_count += 1

    if args.execute:
        conn.commit()

        is_new = not TUNEMYMUSIC_CSV.exists()
        TUNEMYMUSIC_CSV.parent.mkdir(parents=True, exist_ok=True)
        with open(TUNEMYMUSIC_CSV, "a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if is_new:
                writer.writerow(["artist", "title", "tribute_act", "example_tribute_title", "example_original_path"])
            for key in sorted(moved_for_csv):
                info = moved_for_csv[key]
                writer.writerow([
                    info["artist"],
                    info["title"],
                    info["tribute_act"],
                    info["example_tribute_title"],
                    info["example_original_path"],
                ])
        print(f"\nWrote {len(moved_for_csv)} unique-song row(s) to {TUNEMYMUSIC_CSV}")

    conn.close()

    print(f"\n{'Moved' if args.execute else 'Would move'}: {moved_count}/{len(found)} row(s)")
    print(f"Unique songs: {len(moved_for_csv)}")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for e in errors:
            print(f"  {e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
