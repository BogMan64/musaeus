#!/usr/bin/env python3
"""
MUSAEUS — Convergence Report  (read-only)

Grey's plan, 2026-09-09: run the whole pipeline, correct what it finds, run
it again, and keep going until it stops changing things. This answers the
question that plan turns on — *did this pass change less than the last one?*
— because without it "still working" and "stuck in a loop" look identical
from the outside.

The failure mode it exists to catch is oscillation. One stage renames A to
B; another renames B back to A. Every pass reports activity, every pass
looks productive, and the library never settles. A falling change count is
convergence. A flat change count with the same files in it is a loop.

WHAT COUNTS AS A CHANGE
This is the whole difficulty, and it is answered from the data rather than
from a hand-kept list of event types -- MUSAEUS already has six authorities
that can disagree about a genre and does not need a seventh that disagrees
about what a change is. Every event falls into one of three buckets by
what it actually recorded:

  TRANSITION   old_value and new_value both present and different.
               A change with a before and an after. Fully checkable, and
               the only bucket oscillation can be detected in.

  BLIND WRITE  new_value present, old_value empty.
               Something was written; the event does not say whether it
               differed from what was there. TAGGER_WRITE and FORGE_TAG
               live here.

  OBSERVATION  neither present. INGEST, METADATA_EXTRACTED, the various
               *_FOUND detections. Nothing was changed.

The distinction matters more than it looks. A pass that has converged
still emits observations -- every file is still hashed and probed -- so
counting all events would show a library that never settles. And BLIND
WRITE is a genuine blind spot, stated rather than hidden: a tagger writing
byte-identical tags on every pass is indistinguishable here from one doing
real work. Convergence is judged on TRANSITIONS.

Read-only. Opens the database with mode=ro and writes nothing, ever.

Usage:
    python3 scripts/convergence_report.py                 # last 5 pipeline runs
    python3 scripts/convergence_report.py --runs 10
    python3 scripts/convergence_report.py --oscillations   # the loop detector alone
    python3 scripts/convergence_report.py --csv out.csv    # oscillating files
"""

from __future__ import annotations

import argparse
import collections
import csv
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.config import MusicConfig  # noqa: E402

# A pipeline run's id is stamped by cli.py as run_<UTC timestamp>_<digest>.
# Hand-made ids from one-off repair scripts (grey_delete_..., phantom_dupe_
# clear_...) are deliberately NOT pipeline passes and would make the trend
# meaningless if counted as one.
RUN_PREFIX = "run_"

TRANSITION = "transition"
BLIND = "blind write"
OBSERVATION = "observation"


def bucket(old: str | None, new: str | None) -> str:
    o, n = (old or "").strip(), (new or "").strip()
    if o and n:
        return TRANSITION if o != n else OBSERVATION
    if n:
        return BLIND
    return OBSERVATION


def pipeline_runs(conn: sqlite3.Connection, limit: int) -> list[tuple[str, str]]:
    """The last `limit` pipeline runs, oldest first so the trend reads left to right."""
    rows = conn.execute(
        "SELECT run_id, MIN(ts) started FROM events WHERE run_id LIKE ? "
        "GROUP BY run_id ORDER BY started DESC LIMIT ?",
        (RUN_PREFIX + "%", limit),
    ).fetchall()
    return [(r["run_id"], r["started"]) for r in reversed(rows)]


def counts_for(conn: sqlite3.Connection, run_id: str) -> collections.Counter:
    c: collections.Counter = collections.Counter()
    for r in conn.execute("SELECT old_value, new_value FROM events WHERE run_id = ?", (run_id,)):
        c[bucket(r["old_value"], r["new_value"])] += 1
    return c


def find_oscillations(conn: sqlite3.Connection, runs: list[str]) -> list[dict]:
    """A file whose value came BACK to something it already had.

    Restricted to transitions, because a value that returns can only be seen
    where the event recorded what the value was. Keyed by file and event
    type: a file whose artist changes while its genre also changes is two
    independent stories, and merging them would invent a loop that is not
    there.
    """
    if not runs:
        return []
    marks = ",".join("?" * len(runs))
    seen: dict[tuple[str, str], list[tuple[str, str, str]]] = collections.defaultdict(list)
    for r in conn.execute(
        f"SELECT run_id, file_path, event_type, old_value, new_value FROM events "  # noqa: S608
        f"WHERE run_id IN ({marks}) AND COALESCE(file_path,'') <> '' "
        f"ORDER BY id",
        runs,
    ):
        if bucket(r["old_value"], r["new_value"]) != TRANSITION:
            continue
        seen[(r["file_path"], r["event_type"])].append(
            (r["run_id"], r["old_value"], r["new_value"])
        )

    out = []
    for (path, etype), hist in seen.items():
        if len(hist) < 2:
            continue
        # The chain has to START at the first recorded old_value. A two-pass
        # loop is old -> new -> old, so a detector that looked only at the
        # new_values would never see the value come back: it would compare
        # ["B", "A"] and find no repeat. That was this function's first bug,
        # and its own tests caught it.
        chain = [hist[0][1]] + [h[2] for h in hist]
        for i, v in enumerate(chain):
            if v in chain[:i]:
                out.append(
                    {
                        "file_path": path,
                        "event_type": etype,
                        "passes": len(hist),
                        "value_cycle": " -> ".join(chain),
                        "runs": " ".join(h[0][:24] for h in hist),
                    }
                )
                break
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--runs", type=int, default=5, help="how many pipeline runs to compare")
    ap.add_argument("--oscillations", action="store_true", help="only the loop detector")
    ap.add_argument("--csv", metavar="PATH", help="write oscillating files to a CSV")
    args = ap.parse_args()

    cfg = MusicConfig.from_env()
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    runs = pipeline_runs(conn, args.runs)
    if not runs:
        print(
            "No pipeline runs found. Runs are recorded with a run_ id by "
            "`musaeus run`; one-off repair scripts use their own ids and "
            "are deliberately not counted as passes."
        )
        return 0

    if not args.oscillations:
        print("\n  PASS-BY-PASS CHANGE COUNT")
        print("  " + "-" * 68)
        print(f"  {'run':<26} {'changes':>10} {'blind writes':>12} {'observed':>12}")
        prev = None
        for run_id, started in runs:
            c = counts_for(conn, run_id)
            t = c[TRANSITION]
            arrow = ""
            if prev is not None:
                if t == 0 and prev == 0:
                    # Flat at zero is the destination, not a warning. Saying
                    # "check for a loop" here would train the reader to
                    # ignore the line that matters.
                    arrow = "  ✓ settled — nothing changed"
                elif t < prev:
                    arrow = "  ↓ converging"
                elif t > prev:
                    arrow = "  ↑ MORE than last pass"
                else:
                    arrow = "  → flat at " + f"{t:,}" + " — check for a loop"
            elif t == 0:
                arrow = "  ✓ nothing changed"
            print(f"  {started[:19]:<26} {t:>10,} {c[BLIND]:>12,} {c[OBSERVATION]:>12,}{arrow}")
            prev = t
        print()
        print("  'changes' is the number to watch: events that recorded a before")
        print("  AND an after, and the two differed. Falling to zero is convergence.")
        print("  'blind writes' recorded no prior value, so whether they changed")
        print("  anything cannot be told from here -- that is a gap, not a zero.")

    osc = find_oscillations(conn, [r for r, _ in runs])
    print(f"\n  OSCILLATION CHECK  ({len(runs)} pass(es) examined)")
    print("  " + "-" * 68)
    if not osc:
        print("  No file returned to a value it already had. Nothing is looping.")
    else:
        print(f"  {len(osc)} file(s) changed to a value they ALREADY HAD in an")
        print("  earlier pass. That is a loop: more passes will not settle these.\n")
        for o in sorted(osc, key=lambda x: -x["passes"])[:15]:
            print(f"   {o['event_type'][:28]:<28} {Path(o['file_path']).name[:46]}")
            print(f"      {o['value_cycle'][:100]}")
        if len(osc) > 15:
            print(f"\n   … and {len(osc) - 15} more")

    if args.csv and osc:
        out = Path(args.csv)
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(osc[0].keys()))
            w.writeheader()
            w.writerows(osc)
        print(f"\n  oscillating files -> {out}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
