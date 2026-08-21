#!/usr/bin/env python3
"""
MUSAEUS — enforce the mono/stereo-only library rule.

Grey's rule, 2026-08-21: "Mono and stereo versions only, anything else
should be added to the TuneMyMusic.csv and the physical file deleted."

Why the library cannot just keep them: the AVR target and the whole
loudness pipeline are built for 2-channel audio. BPMStage already skips
multichannel outright, and the earlier TuneMyMusic.csv entries carry the
reason "multichannel audio (no stereo interest -- see BPM skip)" -- so
these files sit in the library consuming space while being excluded from
half the pipeline. Logging them for re-acquisition in stereo and removing
the 5.1 copy is the resolution.

This DELETES audio, which nothing else in MUSAEUS does. Accordingly:

  - It only ever touches rows where the probed channel count is > 2.
    Mono (1) and stereo (2) are never candidates.
  - It re-probes each file with ffprobe immediately before deleting and
    refuses to act if the live channel count disagrees with the DB. A
    stale row must never be the reason a file is destroyed.
  - Every file is written to TuneMyMusic.csv BEFORE it is deleted, so the
    re-source record exists even if the run dies midway.
  - The archive row and its hash-ledger entry go too, otherwise
    AuditStage would correctly start failing on a finalized row whose
    file is missing.
  - --check reports exactly what --apply would do.

Usage:
    python3 scripts/musaeus_purge_multichannel.py --check
    python3 scripts/musaeus_purge_multichannel.py --apply
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.config import MusicConfig, get_config  # noqa: E402

CSV_FIELDS = ["reason", "codec", "bitrate_kbps", "sample_rate", "channels", "duration_sec", "path"]


def _probe_channels(path: Path) -> int | None:
    """Live channel count, or None if it cannot be determined."""
    r = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=channels",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    # ffprobe's csv writer still emits a field separator when the file
    # carries a second stream (these releases embed cover art as mjpeg),
    # so a bare isdigit() check rejects a perfectly good "6," and would
    # silently spare files this rule is meant to catch. Take the first
    # field rather than demanding the whole string be a number.
    first = r.stdout.strip().split(",")[0].strip()
    return int(first) if first.isdigit() else None


def _purge_row(
    conn: sqlite3.Connection,
    cfg: MusicConfig,
    row: sqlite3.Row,
    *,
    channels: int | None,
    note: str,
) -> None:
    """Remove one archive row, its duplicates entries and its ledger entry.

    All three together: a row removed while its ledger entry survives is
    precisely the condition that produced the 2026-08-17/18 dupe cascade
    (scope doc section 4.17), where a stale hash outlived the file it named
    and quarantined the next honest copy of that audio.
    """
    path = str(row["file_path"])
    conn.execute("DELETE FROM archive WHERE rowid = ?", (row["rid"],))
    conn.execute("DELETE FROM duplicates WHERE file_path = ?", (path,))
    conn.execute(
        """
        INSERT INTO events (run_id, ts, event_type, file_path, stage, note)
        VALUES (?, ?, 'MULTICHANNEL_PURGED', ?, 'purge-multichannel', ?)
        """,
        (
            "manual",
            datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            path,
            f"{channels}ch removed -- {note}",
        ),
    )
    conn.commit()

    if row["audio_hash"] and cfg.hash_index_path.exists():
        idx = sqlite3.connect(cfg.hash_index_path)
        idx.execute("DELETE FROM finalized_hashes WHERE file_path = ?", (path,))
        idx.commit()
        idx.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="Report only, delete nothing")
    g.add_argument("--apply", action="store_true", help="Log to CSV, then delete")
    ap.add_argument(
        "--include-quarantine",
        action="store_true",
        help="Also purge multichannel files sitting in DUPES_MOVED_FOR_REVIEW",
    )
    args = ap.parse_args()

    cfg = get_config()
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row

    statuses = ("CATALOGUED", "DUPE_REVIEW") if args.include_quarantine else ("CATALOGUED",)
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"""
        SELECT rowid AS rid, file_path, artist, title, album, codec, bitrate,
               sample_rate, channels, duration, audio_hash, status
          FROM archive
         WHERE status IN ({placeholders}) AND channels > 2
         ORDER BY artist, title
        """,
        statuses,
    ).fetchall()

    print(f"multichannel rows (>2 channels): {len(rows)}")
    if not rows:
        return 0

    csv_path = cfg.alac_library / "TuneMyMusic.csv"
    known = set()
    if csv_path.exists():
        known = {r["path"] for r in csv.DictReader(csv_path.open())}

    deleted = logged = mismatch = missing = 0
    freed = 0

    for row in rows:
        path = Path(row["file_path"])

        # A row whose file is already gone still needs its row removed.
        # This is exactly how the aborted first run left two files, and
        # leaving the rows would keep AuditStage failing on finalized rows
        # with nothing behind them.
        if not path.exists():
            missing += 1
            if not args.check:
                _purge_row(conn, cfg, row, channels=row["channels"], note="file already gone")
            continue

        # Re-probe: never destroy a file on the strength of a DB row alone.
        live = _probe_channels(path)
        if live is None or live <= 2:
            mismatch += 1
            print(f"  REFUSED (db says {row['channels']}ch, file says {live}): {path.name}")
            continue

        if args.check:
            print(f"  would remove: {live}ch  {row['artist']} - {row['title']}")
            continue

        entry = {
            "reason": (
                f"{live}-channel audio -- library is mono/stereo only; "
                "re-source a stereo version (physical file deleted)"
            ),
            "codec": (row["codec"] or "").upper(),
            "bitrate_kbps": int((row["bitrate"] or 0) / 1000) or "",
            "sample_rate": row["sample_rate"] or "",
            "channels": live,
            "duration_sec": round(row["duration"], 1) if row["duration"] else "",
            "path": str(path),
        }

        # CSV first: the re-source record must survive a crash mid-run.
        if str(path) not in known:
            write_header = not csv_path.exists() or csv_path.stat().st_size == 0
            with csv_path.open("a", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
                if write_header:
                    w.writeheader()
                w.writerow(entry)
            known.add(str(path))
            logged += 1

        size = path.stat().st_size

        # DB first, unlink last -- the reverse of SOP 4.12's move ordering,
        # and deliberately so. A move can be undone; a delete cannot. If the
        # DB work throws here the file simply survives and the next run
        # retries it. The first version of this script unlinked first and
        # then hit a schema mismatch on the events insert, which left two
        # files gone with their rows still claiming them present.
        _purge_row(conn, cfg, row, channels=live, note="logged to TuneMyMusic.csv for re-source")

        path.unlink()
        freed += size
        deleted += 1

    print(f"\nlogged to CSV: {logged}   deleted: {deleted}   freed: {freed / 1e9:.2f} GB")
    if mismatch:
        print(f"refused on channel mismatch: {mismatch}")
    if missing:
        print(f"already gone from disk: {missing}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
