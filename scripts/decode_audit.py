#!/usr/bin/env python3
"""
MUSAEUS — Decode Audit

Decodes every CATALOGUED file that has never been decode-checked, records
the result, and MOVES NOTHING.

Why this exists
---------------
`CorruptStage` decodes files its shape heuristic flags as suspect, plus a
bounded `NEW_ARRIVAL_DECODE_BUDGET = 200` never-checked files per run. That
bound is deliberate and correct for a pipeline stage -- the comment there
records that the size-ratio heuristic flagged 418 files of which only 2 were
damaged, and 91 were undamaged Bing Crosby and Count Basie mono recordings
that genuinely compress that far. Acting on the ratio alone would have
quarantined ~91 good masters.

But it means the stage would need ~60 runs to cover a library where 74% has
never been decode-checked (11,917 of 16,107 on 2026-09-08). This script does
that sweep once, as an audit rather than a stage.

What it catches that nothing else can
-------------------------------------
Damage INSIDE the stream of a right-sized, right-duration file. The
container header still says 5:10; only decoding reveals that it stops at
13 seconds. Four such masters surfaced on 2026-09-06 purely because the
LUFS bake happened to touch them -- Billy Joel, Nina Simone, BTO and Tower
of Power, all reporting `[alac] Error` or a truncated mov atom.

Safety
------
- Read-only on audio. It decodes to /dev/null and never writes, moves,
  renames or quarantines a file.
- The only database writes are `decode_ok`, `decode_checked_at` and
  `decode_errors` on rows it has actually checked, plus one event per
  FAILURE. That is the same bookkeeping CorruptStage does.
- Resumable: it only looks at rows where `decode_checked_at IS NULL`, so an
  interrupted run costs nothing. Ctrl-C is safe.
- Commits every 25 files, so a kill loses at most 25 results.

Usage:
    python3 scripts/decode_audit.py                # scan, write results
    python3 scripts/decode_audit.py --limit 50     # try a small batch first
    python3 scripts/decode_audit.py --dry-run      # count only, decode nothing
"""

from __future__ import annotations

import argparse
import csv
import datetime
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.config import MusicConfig  # noqa: E402
from musaeus.deep_scan import ensure_columns as deep_scan_ensure_columns  # noqa: E402

COMMIT_EVERY = 25
_stop = False


def _on_signal(signum, frame):  # noqa: ARG001
    """Finish the file in flight, commit, and exit cleanly."""
    global _stop
    _stop = True
    print("\n  interrupt received — finishing the current file and committing…",
          flush=True)


def decode(path: Path, timeout: int = 900) -> tuple[bool, str]:
    """Decode the whole file to nothing. True when ffmpeg reports no error.

    Deliberately a FULL decode, not `-t 30`: the damage this is looking for
    sits in the middle of the stream, which is exactly what a partial decode
    would miss.
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timed out after %ds" % timeout
    err = (r.stderr or "").strip()
    return (not err), err.splitlines()[0][:200] if err else ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="stop after N files")
    ap.add_argument("--dry-run", action="store_true",
                    help="report how many would be checked, decode nothing")
    args = ap.parse_args()

    cfg = MusicConfig.from_env()
    conn = sqlite3.connect(str(cfg.db_path), timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=120000")
    deep_scan_ensure_columns(conn)

    rows = conn.execute(
        "SELECT id, artist, title, file_path, duration, size_bytes, codec "
        "FROM archive WHERE status='CATALOGUED' AND decode_checked_at IS NULL "
        "ORDER BY id"
    ).fetchall()
    total_cat = conn.execute(
        "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'").fetchone()[0]
    print("catalogued: %d    never decode-checked: %d (%.1f%%)"
          % (total_cat, len(rows), 100 * len(rows) / max(total_cat, 1)))
    if args.limit:
        rows = rows[:args.limit]
    if args.dry_run or not rows:
        print("nothing to do." if not rows else
              "DRY RUN — %d file(s) would be decoded. Nothing was read or written."
              % len(rows))
        conn.close()
        return 0

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    run_id = "decode_audit_" + datetime.datetime.now().strftime("%Y%m%dT%H%M%SZ")
    failures: list[dict] = []
    ok = bad = gone = 0
    started = time.time()

    for i, r in enumerate(rows, 1):
        if _stop:
            break
        p = Path(r["file_path"])
        if not p.exists():
            gone += 1
            continue
        good, err = decode(p)
        ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE archive SET decode_checked_at = ?, decode_ok = ?, decode_errors = ? "
            "WHERE id = ?", (ts, 1 if good else 0, 0 if good else 1, r["id"]))
        if good:
            ok += 1
        else:
            bad += 1
            failures.append({
                "artist": r["artist"] or "", "title": r["title"] or "",
                "duration_s": "%.0f" % (r["duration"] or 0),
                "size_MB": "%.1f" % ((r["size_bytes"] or 0) / 1048576),
                "codec": r["codec"] or "", "error": err, "path": str(p)})
            conn.execute(
                "INSERT INTO events(run_id,ts,event_type,file_path,old_value,new_value,"
                "stage,note) VALUES(?,?,?,?,?,?,?,?)",
                (run_id, ts, "DECODE_FAILED", str(p), "", "", "decode_audit",
                 "full-decode audit found damage inside the stream: %s" % err))
            print("  ✗ %-24s %-40s %s"
                  % ((r["artist"] or "")[:24], (r["title"] or "")[:40], err[:60]),
                  flush=True)
        if i % COMMIT_EVERY == 0:
            conn.commit()
            rate = i / max(time.time() - started, 1)
            left = (len(rows) - i) / max(rate, 0.001) / 3600
            print("  … %d/%d  ok=%d bad=%d  %.1f files/s  ~%.1f h left"
                  % (i, len(rows), ok, bad, rate, left), flush=True)

    conn.commit()
    checked = ok + bad
    print("\ndecoded %d file(s): %d clean, %d DAMAGED%s"
          % (checked, ok, bad, ", %d missing on disk" % gone if gone else ""))
    if _stop:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' "
            "AND decode_checked_at IS NULL").fetchone()[0]
        print("stopped early — %d still unchecked. Re-run to continue where it left off."
              % remaining)

    if failures:
        out = Path("/home/grey/Desktop/MUSAEUS_decode_failures_%s.csv"
                   % datetime.date.today().isoformat())
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["DELETE? (y/n)"] + list(failures[0].keys()))
            w.writeheader()
            for f in failures:
                w.writerow({"DELETE? (y/n)": "", **f})
        print("failures -> %s" % out)
        print("NOTHING was moved or deleted. Every file is where it was.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
