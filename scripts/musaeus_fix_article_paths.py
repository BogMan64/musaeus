"""Relocate CATALOGUED files whose path still carries the old "(the)" form.

Collision-safe per SOP §4.12: disk move first, DB update by rowid second,
revert the disk move if the DB write fails. Never overwrites an existing file.
"""
import sqlite3, sys
from pathlib import Path

DB = "/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/musaeus.db"
APPLY = "--apply" in sys.argv

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT id, artist, file_path FROM archive "
    "WHERE status='CATALOGUED' AND file_path LIKE '%(the)%'"
).fetchall()

moved = skipped = failed = 0
for r in rows:
    src = Path(r["file_path"])
    if not src.exists():
        print(f"SKIP missing: {src}"); skipped += 1; continue

    parts = list(src.parts)
    idx = next((i for i, s in enumerate(parts) if "(the)" in s.lower()), None)
    if idx is None:
        skipped += 1; continue
    parts[idx] = r["artist"]                      # corrected artist folder
    dst = Path(*parts)
    # filename also embeds the artist -- keep it consistent with the folder
    if "(the)" in dst.name.lower():
        old_prefix = src.name.split(" - ")[0]
        if "(the)" in old_prefix.lower():
            dst = dst.with_name(dst.name.replace(old_prefix, r["artist"], 1))
    if dst == src:
        skipped += 1; continue
    if dst.exists():
        print(f"SKIP target exists: {dst}"); skipped += 1; continue

    if not APPLY:
        print(f"WOULD MOVE\n   {src}\n-> {dst}"); moved += 1; continue

    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)                                # disk first
    try:
        conn.execute("UPDATE archive SET file_path=? WHERE rowid=?", (str(dst), r["id"]))
        conn.commit()
        moved += 1
    except Exception as exc:                       # revert disk on DB failure
        dst.rename(src)
        print(f"DB WRITE FAILED, reverted: {src}: {exc}")
        failed += 1

# prune the now-empty old artist dirs
if APPLY:
    lib = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/ALAC-Library")
    for d in sorted((p for p in lib.rglob("*") if p.is_dir() and "(the)" in p.name.lower()),
                    key=lambda p: len(p.parts), reverse=True):
        if "DUPES_MOVED_FOR_REVIEW" in d.parts:
            continue
        try:
            next(d.rglob("*"))
        except StopIteration:
            d.rmdir()
            print(f"pruned empty: {d}")

print(f"\n{'APPLIED' if APPLY else 'DRY RUN'}  moved={moved} skipped={skipped} failed={failed}")
conn.close()
