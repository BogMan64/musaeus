#!/usr/bin/env python3
"""
Measure drift between the finalized-hash ledger and the filesystem.

finalized_hashes.file_path records where a file was at the moment it was
finalized. Nothing reconciles it when the file later moves, so after a
relocation the ledger names paths that no longer exist. cross_dupe treats
such a hit as unusable and continues (fails OPEN) rather than trusting it
-- see cross_dupe.py and scope doc 4.17 for why: acting on an unverified
hit is what produced the 2026-08-17/18 cascade, in which 10,597 tracks
were quarantined as duplicates of themselves.

This script quantifies the drift, and in particular answers the question
that decides whether fail-open is a defect or a load-bearing design
choice:

    how many files would a fail-CLOSED cross_dupe wrongly quarantine?

These figures were measured from a 2026-08-26T09:31Z snapshot. They are
a PRE-PURGE baseline: later that day an empty-genre purge hard-deleted 42
files and their archive rows but left 36 of their finalized_hashes rows
behind, so a ledger read after ~17:22Z carries 36 orphans that are
deletions rather than drift. Re-derive the baseline after those are
cleaned rather than comparing across that line.

That count is the headline output. On the 2026-08-26 batch it was 849 of
2,028 candidates (41.9%), of which 848 had exactly one dead twin -- i.e.
they were the same audio re-finalized into a new dated folder, and
failing closed would have rejected each as a duplicate of its own former
self. Exactly one hash in a 12,221-hash ledger had two dead paths and
could conceivably have been a real duplicate.

The shape breakdown says where the drift comes from. On that batch:
746 same path below a different dated folder, 67 renamed, 32 moved into
a holding area, 4 same filename relocated. The holding-area group is
worth watching -- those candidates' dead twins carry the CURRENT date,
so dupe_resolver moved the file during this batch and left the ledger
naming the pre-move path. The drift is not only a historical scar; the
pipeline manufactures more of it on every run.

Read-only. It never writes to either database.

WHY IT SNAPSHOTS BY DEFAULT
    The vault DB is held open by a live console with a WAL attached.
    Querying it directly is safe but gives a moving target, so by default
    this takes an online .backup into a temp dir and reads that.

    Do NOT reach for `immutable=1` to read a live database. It does not
    mean "consistent but possibly stale" -- it means the WAL is ignored
    entirely, so it returns silently wrong answers on an active DB, up to
    and including reporting that a table does not exist. Tested against
    this project's live DB with a writer attached:

        mode=ro              -> sees committed + in-WAL rows
        mode=ro&immutable=1  -> ERROR: no such table: t

    A confident wrong answer with no error is the exact failure shape
    this project keeps cataloguing. Snapshot, or plain mode=ro. Never
    immutable.

SCOPE
    Reports. It does not prune the ledger, and it does not reconcile
    file_path -- the durable fix (scope doc 4.24) is to update the ledger
    when finalize moves a file, which belongs in finalize, not here.

Run:  python3 scripts/musaeus_ledger_drift.py [--live] [--json]
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.config import get_config  # noqa: E402

_DATE_DIR = re.compile(r"/(\d{4}-\d{2}-\d{2})/")

# Directories a file is moved INTO after it has already been finalized.
# A dead twin whose candidate now sits in one of these is drift the
# pipeline generated during this very batch, not a historical relocation.
_HOLDING = ("DUPES_MOVED_FOR_REVIEW", "TRIBUTE_REMOVED_FOR_REVIEW", "QUARANTINE")


def _open_ro(path: Path) -> sqlite3.Connection:
    """Open *path* read-only. Plain mode=ro -- never immutable; see module docstring."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _snapshot(src: Path, dest: Path) -> sqlite3.Connection:
    """Online-backup *src* to *dest* and return a read-only handle on the copy.

    Uses the same online-backup API as `sqlite3 db ".backup out"`, which
    folds in the WAL and is safe against a live writer.
    """
    source = _open_ro(src)
    target = sqlite3.connect(str(dest))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return _open_ro(dest)


def _load_ledger(conn: sqlite3.Connection) -> dict[str, set[str]]:
    ledger: dict[str, set[str]] = collections.defaultdict(set)
    for row in conn.execute("SELECT audio_hash, file_path FROM finalized_hashes"):
        ledger[row["audio_hash"]].add(row["file_path"])
    return ledger


def _candidates(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """The rows cross_dupe would check -- same query as its _get_candidates."""
    try:
        rows = conn.execute(
            """
            SELECT a.file_path, a.audio_hash
              FROM archive a
             WHERE a.audio_hash IS NOT NULL
               AND NOT EXISTS (
                     SELECT 1 FROM duplicates d
                      WHERE d.file_path = a.file_path
                        AND d.duplicate_type = 'CROSS_BATCH'
                   )
             ORDER BY a.file_path
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # no batch in flight
    return [(r["file_path"], r["audio_hash"]) for r in rows]


def _tail(path: str) -> tuple[str | None, str]:
    """Split a library path at its dated folder: ('2026-08-18', 'Artist/Album/x.m4a')."""
    m = _DATE_DIR.search(path)
    return (m.group(1), path[m.end():]) if m else (None, path)


def analyse(ledger: dict[str, set[str]], cands: list[tuple[str, str]]) -> dict:
    paths = {p for ps in ledger.values() for p in ps}
    live = {p for p in paths if os.path.exists(p)}

    zero_live = {h: ps for h, ps in ledger.items() if not (ps & live)}
    zl_multi = {h: ps for h, ps in zero_live.items() if len(ps) > 1}

    # replay cross_dupe branch for branch
    counts = collections.Counter()
    shape = collections.Counter()
    twin_dates: collections.Counter = collections.Counter()
    examples: list[dict] = []

    for path_str, ah in cands:
        entries = ledger.get(ah)
        if not entries:
            counts["no_ledger_entry"] += 1
            continue
        twins = entries - {path_str}
        if not twins:
            counts["own_path_only"] += 1
            continue
        if twins & live:
            counts["cross_batch_match"] += 1
            continue

        counts["would_fail_closed"] += 1
        counts["single_dead_twin" if len(twins) == 1 else "multi_dead_twin"] += 1

        twin = sorted(twins)[0]
        tdate, ttail = _tail(twin)
        twin_dates[tdate] += 1
        _, ctail = _tail(path_str)
        if any(f"/{h}/" in path_str for h in _HOLDING):
            shape["moved_to_holding_area"] += 1
        elif ctail == ttail:
            shape["same_path_below_date_dir"] += 1
        elif os.path.basename(twin) == os.path.basename(path_str):
            shape["same_filename_moved"] += 1
        else:
            shape["renamed"] += 1
            if len(examples) < 5:
                examples.append({"candidate": path_str, "dead_twin": twin})

    return {
        "ledger": {
            "rows": sum(len(v) for v in ledger.values()),
            "distinct_hashes": len(ledger),
            "distinct_paths": len(paths),
            "paths_live": len(live),
            "paths_dead": len(paths) - len(live),
            "hashes_with_no_live_path": len(zero_live),
            "hashes_no_live_multi_path": len(zl_multi),
        },
        "batch": {"candidates": len(cands), **counts},
        "dead_twin_shape": dict(shape),
        "dead_twin_date_dirs": dict(sorted(twin_dates.items(), key=lambda kv: str(kv[0]))),
        "renamed_examples": examples,
    }


def report(a: dict) -> None:
    L, B = a["ledger"], a["batch"]
    n = B["candidates"]
    fc = B.get("would_fail_closed", 0)
    pct = f"{100 * fc / n:.1f}%" if n else "n/a"

    print("╭─ WOULD-FAIL-CLOSED " + "─" * 44)
    print(f"│  {fc} of {n} candidates ({pct})")
    print("│  files a fail-closed cross_dupe would quarantine as")
    print("│  duplicates of their own former selves")
    print("╰" + "─" * 64)
    print()
    print("LEDGER")
    print(f"  rows                        {L['rows']}")
    print(f"  distinct hashes             {L['distinct_hashes']}")
    print(f"  distinct paths              {L['distinct_paths']}")
    print(f"    live on disk              {L['paths_live']}")
    print(f"    dead                      {L['paths_dead']}")
    print(f"  hashes with no live path    {L['hashes_with_no_live_path']}")
    print(f"    of those, >=2 dead paths  {L['hashes_no_live_multi_path']}"
          "   <- only these could be real duplicates")
    print()
    print(f"BATCH  ({n} candidates, cross_dupe branch for branch)")
    for k, label in (
        ("no_ledger_entry", "no ledger entry"),
        ("own_path_only", "only own path in ledger -> continue"),
        ("would_fail_closed", "STALE -> fails open"),
        ("single_dead_twin", "  single dead twin (self-dupe if closed)"),
        ("multi_dead_twin", "  >=2 dead twins"),
        ("cross_batch_match", "real CROSS_BATCH match"),
    ):
        print(f"  {label:<42}{B.get(k, 0)}")
    if a["dead_twin_shape"]:
        print()
        print("HOW THE DEAD TWIN DIFFERS")
        for k, v in sorted(a["dead_twin_shape"].items(), key=lambda kv: -kv[1]):
            print(f"  {k:<42}{v}")
    if a["dead_twin_date_dirs"]:
        print()
        print("DEAD TWIN DATE DIRS")
        for k, v in a["dead_twin_date_dirs"].items():
            print(f"  {str(k):<42}{v}")
    if a["renamed_examples"]:
        print()
        print("RENAMED EXAMPLES")
        for ex in a["renamed_examples"]:
            print(f"  cand {ex['candidate']}")
            print(f"  twin {ex['dead_twin']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--live", action="store_true",
                    help="query the live DBs directly instead of snapshotting "
                         "(read-only either way; the snapshot just gives a stable view)")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()

    cfg = get_config()
    idx_path, db_path = cfg.hash_index_path, cfg.db_path
    if not idx_path.exists():
        print(f"no hash index at {idx_path} — nothing to measure", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="ledger_drift_") as tmp:
        if args.live:
            idx = _open_ro(idx_path)
            db = _open_ro(db_path) if db_path.exists() else None
        else:
            idx = _snapshot(idx_path, Path(tmp) / "idx.db")
            db = _snapshot(db_path, Path(tmp) / "vault.db") if db_path.exists() else None

        try:
            result = analyse(_load_ledger(idx), _candidates(db) if db else [])
        finally:
            idx.close()
            if db:
                db.close()

    print(json.dumps(result, indent=2)) if args.json else report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
