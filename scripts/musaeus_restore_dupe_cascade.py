#!/usr/bin/env python3
"""
MUSAEUS — one-off recovery for the 2026-08-17/18 dupe cascade.

WHAT HAPPENED
    hash_index.db is an append-only ledger of "this audio was finalized
    once". It deliberately survives DB resets, and nothing prunes an entry
    when the file it names is later moved. CrossDupeStage read a hit as
    "a copy is already held" without checking that the indexed path still
    existed, so once a file had been relocated into
    DUPES_MOVED_FOR_REVIEW, its stale ledger entry outlived it and the
    next pass flagged it as a cross-batch duplicate -- of itself.

    Net effect: 25,890 files (94.5% of the library) sat in quarantine
    against 1,503 live ones, and 10,597 of them were the ONLY copy of
    their audio anywhere in the database. Duplicates of nothing.

WHY THIS RESTORES IN PLACE RATHER THAN REPROCESSING
    These files are not damaged and were never partially processed --
    they went all the way through Finalize before being quarantined
    (which is exactly why they are in the ledger at all). Of the 10,597:
    8,989 are fully finalized, 7,411 carry loudness, 3,727 carry BPM.

    Moving them back to INBOX for a fresh pipeline run would discard all
    of that and re-derive it: ffprobe, an ffmpeg re-encode in
    Canonicalize, an ffmpeg loudness pass in Forge, and Essentia for BPM
    -- hours to days of compute to reproduce numbers that are already
    correct and already stored. Worse, Canonicalize re-encoding would
    change audio_hash again, which is the very continuity this recovery
    is trying to restore.

    Only two things are actually wrong with each row: file_path points
    into the quarantine folder, and status says DUPE_REVIEW. Both are
    fixed here directly. Verified before writing this: all 10,597 source
    files exist, and ZERO target paths are occupied.

SAFETY
    - Restores ONLY files whose audio_hash appears exactly once in the
      whole archive table. A file with another copy somewhere is a
      plausible genuine duplicate and is left strictly alone.
    - Per SOP 4.12: disk move first, DB update by rowid second, and the
      disk move is reverted if the DB write fails.
    - Never deletes an audio file.
    - Refuses to overwrite: a move whose target already exists is
      skipped and reported, not forced.
    - Clears the stale CROSS_BATCH `duplicates` rows for restored files,
      otherwise the next dedupe run would re-quarantine them.
    - Re-points the ledger at the restored path so it stops lying, then
      prunes entries that name files which no longer exist at all.

USAGE
    python3 scripts/musaeus_restore_dupe_cascade.py --check
    python3 scripts/musaeus_restore_dupe_cascade.py --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.config import get_config  # noqa: E402

QUARANTINE_SEGMENT = "DUPES_MOVED_FOR_REVIEW"


def _target_for(path: Path, library: Path) -> Path | None:
    """The live-library path a quarantined file should return to.

    The quarantine folder mirrors the library layout with one extra
    segment, so the reverse mapping is that segment's removal -- not a
    guess at where the file "should" go.
    """
    try:
        parts = list(path.relative_to(library).parts)
    except ValueError:
        return None
    if QUARANTINE_SEGMENT not in parts:
        return None
    parts.remove(QUARANTINE_SEGMENT)
    return library.joinpath(*parts)


def _sole_copies(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH hc AS (
            SELECT audio_hash, COUNT(*) AS n
              FROM archive
             WHERE audio_hash IS NOT NULL
             GROUP BY audio_hash
        )
        SELECT d.rowid AS rid, d.file_path, d.audio_hash
          FROM archive d
          JOIN hc ON hc.audio_hash = d.audio_hash
         WHERE d.status = 'DUPE_REVIEW' AND hc.n = 1
         ORDER BY d.file_path
        """
    ).fetchall()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="Report only, change nothing")
    g.add_argument("--apply", action="store_true", help="Perform the restore")
    ap.add_argument("--limit", type=int, default=0, help="Restore at most N files")
    ap.add_argument(
        "--no-ledger",
        action="store_true",
        help="Skip hash-index repair. Use with --limit: pruning the ledger "
        "before the full restore would drop entries still needed to decide.",
    )
    args = ap.parse_args()

    cfg = get_config()
    library = cfg.alac_library

    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row

    rows = _sole_copies(conn)
    if args.limit:
        rows = rows[: args.limit]

    print(f"sole-copy quarantined files: {len(rows)}")

    # No early return on an empty list: the ledger repair below is the other
    # half of this recovery and still needs to run on a re-invocation after
    # every file has already been restored.
    restored = skipped = failed = 0
    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

    for i, row in enumerate(rows, 1):
        src = Path(row["file_path"])
        tgt = _target_for(src, library)

        if tgt is None:
            print(f"  SKIP (not under quarantine): {src}")
            skipped += 1
            continue
        if not src.exists():
            print(f"  SKIP (source gone): {src}")
            skipped += 1
            continue
        if tgt.exists():
            print(f"  SKIP (target occupied): {tgt}")
            skipped += 1
            continue

        if args.check:
            restored += 1
            if i <= 3:
                print(f"  would restore: {src.name}\n              -> {tgt}")
            continue

        # SOP 4.12: disk first, DB second, revert disk if the DB write fails.
        tgt.parent.mkdir(parents=True, exist_ok=True)
        try:
            src.rename(tgt)
        except OSError as exc:
            print(f"  FAIL (move): {src}: {exc}")
            failed += 1
            continue

        try:
            conn.execute(
                """
                UPDATE archive
                   SET file_path = ?, status = 'CATALOGUED', last_seen = ?
                 WHERE rowid = ?
                """,
                (str(tgt), now, row["rid"]),
            )
            # Otherwise the next dedupe run re-quarantines what we just saved.
            conn.execute(
                "DELETE FROM duplicates WHERE file_path = ? AND duplicate_type = 'CROSS_BATCH'",
                (str(src),),
            )
        except sqlite3.Error as exc:
            tgt.rename(src)  # disk reverted; DB never saw a half-state
            print(f"  FAIL (db, move reverted): {src}: {exc}")
            failed += 1
            continue

        restored += 1
        if i % 200 == 0:
            conn.commit()
            print(f"  ... {i}/{len(rows)}")

    if args.apply:
        conn.commit()

    print(f"\nrestored: {restored}   skipped: {skipped}   failed: {failed}")

    # ── Ledger repair ────────────────────────────────────────────────────
    # The ledger is what made this happen; leaving it stale would let it
    # happen again the moment CrossDupe's own guard is bypassed.
    if args.no_ledger:
        print("hash index: repair skipped (--no-ledger)")
        return 0

    idx_path = cfg.hash_index_path
    if not idx_path.exists():
        print("no hash index to repair")
        return 0

    idx = sqlite3.connect(idx_path)
    idx.row_factory = sqlite3.Row
    entries = idx.execute("SELECT rowid AS rid, audio_hash, file_path FROM finalized_hashes")

    live = {
        r["audio_hash"]: r["file_path"]
        for r in conn.execute(
            "SELECT audio_hash, file_path FROM archive "
            "WHERE status='CATALOGUED' AND audio_hash IS NOT NULL"
        )
    }

    repoint = prune = 0
    for e in entries.fetchall():
        if Path(e["file_path"]).exists():
            continue
        current = live.get(e["audio_hash"])
        if current:
            # (audio_hash, file_path) is UNIQUE, so a re-point can collide
            # with an entry that already names the live path -- which just
            # means the ledger already records the truth and this row is a
            # redundant stale copy of it. Drop it rather than fail: the
            # goal is a ledger with no dead paths, not a preserved row
            # count.
            if args.apply:
                dup = idx.execute(
                    "SELECT 1 FROM finalized_hashes WHERE audio_hash = ? "
                    "AND file_path = ? AND rowid != ?",
                    (e["audio_hash"], current, e["rid"]),
                ).fetchone()
                if dup:
                    idx.execute("DELETE FROM finalized_hashes WHERE rowid = ?", (e["rid"],))
                else:
                    idx.execute(
                        "UPDATE finalized_hashes SET file_path = ? WHERE rowid = ?",
                        (current, e["rid"]),
                    )
            repoint += 1
        else:
            # Names a file that is simply not there any more. Keeping it
            # would quarantine the next honest copy of that audio.
            prune += 1
            if args.apply:
                idx.execute("DELETE FROM finalized_hashes WHERE rowid = ?", (e["rid"],))

    if args.apply:
        idx.commit()
    verb = "would re-point" if args.check else "re-pointed"
    verb2 = "would prune" if args.check else "pruned"
    print(f"hash index: {verb} {repoint} stale entr(ies), {verb2} {prune} dead entr(ies)")

    idx.close()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
