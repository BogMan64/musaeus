#!/usr/bin/env python3
"""
MUSAEUS — Dashboard Report
Comprehensive library stats: counts, pipeline progress, genre breakdown,
bitrate histogram, recent activity, and pending work summary.

Usage:
    python3 scripts/musaeus_report.py
    python3 scripts/musaeus_report.py --json       # machine-readable JSON
    python3 scripts/musaeus_report.py --wide       # wider terminal columns

ORPHEUS equivalent: SCRIPTS/orpheus_dashboard.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# Allow running from project root directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.config import get_config
from musaeus.db import open_db

# ── Helpers ───────────────────────────────────────────────────────────────────


def _bar(value: int, total: int, width: int = 30) -> str:
    """ASCII progress bar."""
    if total == 0:
        return " " * width
    filled = int(round(width * value / total))
    return "█" * filled + "░" * (width - filled)


def _pct(value: int, total: int) -> str:
    if total == 0:
        return "  0%"
    return f"{100 * value / total:>3.0f}%"


def _row(label: str, value: int, total: int, width: int = 28) -> str:
    bar = _bar(value, total, width)
    pct = _pct(value, total)
    return f"  {label:<20} {value:>7,}  {bar}  {pct}"


# ── Data gathering ────────────────────────────────────────────────────────────


def _gather(conn) -> dict:  # type: ignore[type-arg]
    d: dict = {}

    # ── Pipeline status ───────────────────────────────────────────────────────
    d["total"] = conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
    d["pending"] = conn.execute("SELECT COUNT(*) FROM archive WHERE status='PENDING'").fetchone()[0]
    d["hashed"] = conn.execute("SELECT COUNT(*) FROM archive WHERE status='HASHED'").fetchone()[0]
    d["catalogued"] = conn.execute(
        "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'"
    ).fetchone()[0]
    d["ghost"] = conn.execute("SELECT COUNT(*) FROM archive WHERE status='GHOST'").fetchone()[0]
    d["forged"] = conn.execute(
        "SELECT COUNT(*) FROM archive WHERE rg_tagged_at IS NOT NULL"
    ).fetchone()[0]
    d["car_export"] = conn.execute(
        "SELECT COUNT(*) FROM archive WHERE car_export_path IS NOT NULL"
    ).fetchone()[0]

    # MB enrichment (columns may not exist yet)
    try:
        d["mb_enriched"] = conn.execute(
            "SELECT COUNT(*) FROM archive WHERE mb_artist_id IS NOT NULL"
        ).fetchone()[0]
    except Exception:
        d["mb_enriched"] = 0

    # Normalised (has NORMALIZE_ARTIST event)
    try:
        d["normalized"] = conn.execute(
            "SELECT COUNT(DISTINCT file_path) FROM events WHERE event_type='NORMALIZE_ARTIST'"
        ).fetchone()[0]
    except Exception:
        d["normalized"] = 0

    # ── Genre breakdown ───────────────────────────────────────────────────────
    genre_rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(trim(genre), ''), '(none)') AS g, COUNT(*) AS cnt
        FROM archive
        WHERE status = 'CATALOGUED'
        GROUP BY g
        ORDER BY cnt DESC
        LIMIT 20
        """
    ).fetchall()
    d["genres"] = [(r["g"], r["cnt"]) for r in genre_rows]

    # No genre
    d["no_genre"] = conn.execute(
        "SELECT COUNT(*) FROM archive "
        "WHERE status='CATALOGUED' AND (genre IS NULL OR trim(genre)='')"
    ).fetchone()[0]

    # ── Bitrate histogram ─────────────────────────────────────────────────────
    bitrate_rows = conn.execute(
        """
        SELECT
          CASE
            WHEN bitrate IS NULL OR bitrate = 0          THEN 'unknown'
            WHEN bitrate < 192                           THEN '< 192 kbps (low)'
            WHEN bitrate >= 192 AND bitrate < 256        THEN '192-255 kbps'
            WHEN bitrate >= 256 AND bitrate < 320        THEN '256-319 kbps'
            WHEN bitrate >= 320 AND bitrate < 700        THEN '320 kbps (lossy-max)'
            WHEN bitrate >= 700 AND bitrate < 1200       THEN '700-1199 kbps (FLAC)'
            ELSE                                              '≥ 1200 kbps (lossless)'
          END AS tier,
          COUNT(*) AS cnt
        FROM archive
        WHERE status = 'CATALOGUED'
        GROUP BY tier
        ORDER BY cnt DESC
        """
    ).fetchall()
    d["bitrates"] = [(r["tier"], r["cnt"]) for r in bitrate_rows]

    # ── Codec breakdown ───────────────────────────────────────────────────────
    codec_rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(trim(codec),''), 'unknown') AS c, COUNT(*) AS cnt
        FROM archive
        WHERE status = 'CATALOGUED'
        GROUP BY c
        ORDER BY cnt DESC
        LIMIT 10
        """
    ).fetchall()
    d["codecs"] = [(r["c"], r["cnt"]) for r in codec_rows]

    # ── Duplicate groups ──────────────────────────────────────────────────────
    # The column is `duplicate_type`, not `type`. It always was -- see
    # db.open_db()'s schema -- and the two queries below asked for `type`
    # until 2026-09-09. That alone would have been a visible crash; what
    # made it invisible was the bare `except Exception` around all three,
    # which caught `no such column: type` from the SECOND query and set all
    # three counts to zero, including the first one, which had succeeded.
    #
    # So this report said "0 dupe groups pending" while the database held
    # 2,862. A wrong column name was being rendered as good news, and the
    # only symptom was the absence of a warning.
    #
    # Each query is now tried on its own, and a query that cannot run
    # records None -- "not known" -- rather than 0. The renderer prints an
    # explicit "count unavailable" for None, because a report that silently
    # substitutes zero for an error is worse than one that fails outright:
    # zero is the answer that makes a reader stop looking.
    for key, sql in (
        ("dupe_pending", "SELECT COUNT(DISTINCT group_id) FROM duplicates WHERE status='pending'"),
        (
            "dupe_exact",
            "SELECT COUNT(DISTINCT group_id) FROM duplicates "
            "WHERE duplicate_type='EXACT' AND status='pending'",
        ),
        (
            "dupe_near",
            "SELECT COUNT(DISTINCT group_id) FROM duplicates "
            "WHERE duplicate_type='NEAR' AND status='pending'",
        ),
    ):
        try:
            d[key] = conn.execute(sql).fetchone()[0]
        except sqlite3.Error:
            d[key] = None

    # ── Health / validation issues ────────────────────────────────────────────
    try:
        d["health_errors"] = conn.execute(
            "SELECT COUNT(*) FROM validation_issues WHERE severity='error'"
        ).fetchone()[0]
        d["health_warnings"] = conn.execute(
            "SELECT COUNT(*) FROM validation_issues WHERE severity='warning'"
        ).fetchone()[0]
    except Exception:
        d["health_errors"] = d["health_warnings"] = 0

    # Auditor flagged (may not exist yet)
    try:
        d["auditor_flagged"] = conn.execute(
            "SELECT COUNT(*) FROM archive WHERE auditor_flagged=1"
        ).fetchone()[0]
    except Exception:
        d["auditor_flagged"] = 0

    # ── Recent activity ───────────────────────────────────────────────────────
    try:
        recent = conn.execute(
            """
            SELECT event_type, COUNT(*) AS cnt, MAX(ts) AS last_ts
            FROM events
            WHERE ts >= datetime('now', '-7 days')
            GROUP BY event_type
            ORDER BY cnt DESC
            LIMIT 12
            """
        ).fetchall()
        d["recent_events"] = [(r["event_type"], r["cnt"], r["last_ts"]) for r in recent]
    except Exception:
        d["recent_events"] = []

    # Last run
    try:
        d["last_run"] = conn.execute(
            "SELECT MAX(ts) FROM events WHERE event_type='RUN_START'"
        ).fetchone()[0]
    except Exception:
        d["last_run"] = None

    # ── Top artists (by track count) ─────────────────────────────────────────
    artist_rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(trim(artist),''), '(unknown)') AS a, COUNT(*) AS cnt
        FROM archive
        WHERE status = 'CATALOGUED'
        GROUP BY a
        ORDER BY cnt DESC
        LIMIT 10
        """
    ).fetchall()
    d["top_artists"] = [(r["a"], r["cnt"]) for r in artist_rows]

    return d


# ── Renderers ─────────────────────────────────────────────────────────────────


def _print_report(d: dict, cfg, wide: bool = False) -> None:
    W = 100 if wide else 72
    sep = "─" * W

    print(f"\n{'MUSAEUS LIBRARY REPORT':^{W}}")
    print(sep)
    print(f"  Vault   : {cfg.vault_root}")
    print(f"  DB      : {cfg.db_path}")
    print(f"  Last run: {d.get('last_run') or 'never'}")
    print()

    # ── Pipeline progress ─────────────────────────────────────────────────────
    total = d["total"]
    cat = d["catalogued"]
    print("  PIPELINE PROGRESS")
    print(f"  {'Total files':<20} {total:>7,}")
    print(_row("PENDING", d["pending"], total))
    print(_row("HASHED", d["hashed"], total))
    print(_row("CATALOGUED", cat, total))
    print(_row("  ↳ Forged", d["forged"], cat))
    print(_row("  ↳ Car export", d["car_export"], cat))
    print(_row("  ↳ MB enriched", d["mb_enriched"], cat))
    print(_row("  ↳ Normalised", d["normalized"], cat))
    if d["ghost"]:
        print(f"  {'GHOST (missing)':<20} {d['ghost']:>7,}  ⚠ run `musaeus ghost`")
    print()

    # ── Pending work ──────────────────────────────────────────────────────────
    pending_items = []
    if d["no_genre"]:
        pending_items.append(f"  ⚠  {d['no_genre']:,} track(s) missing genre  → `musaeus enrich`")
    if d["dupe_pending"] is None:
        pending_items.append(
            "  ?  dupe group count unavailable — the duplicates table did not "
            "answer. This is NOT zero pending; it is unknown."
        )
    elif d["dupe_pending"]:

        def _n(v):
            return "?" if v is None else v

        pending_items.append(
            f"  ⚠  {d['dupe_pending']} dupe group(s) pending "
            f"({_n(d['dupe_exact'])} exact / {_n(d['dupe_near'])} near)  → `musaeus dedupe`"
        )
    if d["health_errors"]:
        pending_items.append(
            f"  ✗  {d['health_errors']} health error(s)  → `musaeus health-report`"
        )
    if d["health_warnings"]:
        pending_items.append(
            f"  ⚠  {d['health_warnings']} health warning(s)  → `musaeus health-report`"
        )
    if d["auditor_flagged"]:
        pending_items.append(
            f"  ⚠  {d['auditor_flagged']} file(s) flagged by auditor (LUFS)  → `musaeus forge`"
        )
    cat_unforg = cat - d["forged"]
    if cat_unforg > 0:
        pending_items.append(
            f"  ·  {cat_unforg:,} CATALOGUED file(s) not yet forged  → `musaeus forge`"
        )

    if pending_items:
        print("  PENDING WORK")
        for item in pending_items:
            print(item)
        print()

    # ── Genre breakdown ───────────────────────────────────────────────────────
    if d["genres"]:
        print("  GENRE BREAKDOWN (top 20, CATALOGUED)")
        max_cnt = max(cnt for _, cnt in d["genres"]) or 1
        for genre, cnt in d["genres"]:
            bar = _bar(cnt, max_cnt, 24)
            print(f"  {genre:<28} {cnt:>6,}  {bar}")
        print()

    # ── Bitrate histogram ─────────────────────────────────────────────────────
    if d["bitrates"]:
        print("  BITRATE BREAKDOWN (CATALOGUED)")
        max_cnt = max(cnt for _, cnt in d["bitrates"]) or 1
        for tier, cnt in d["bitrates"]:
            bar = _bar(cnt, max_cnt, 24)
            print(f"  {tier:<28} {cnt:>6,}  {bar}")
        print()

    # ── Codec breakdown ───────────────────────────────────────────────────────
    if d["codecs"]:
        print("  CODEC BREAKDOWN (CATALOGUED)")
        max_cnt = max(cnt for _, cnt in d["codecs"]) or 1
        for codec, cnt in d["codecs"]:
            bar = _bar(cnt, max_cnt, 24)
            print(f"  {codec:<28} {cnt:>6,}  {bar}")
        print()

    # ── Top artists ───────────────────────────────────────────────────────────
    if d["top_artists"]:
        print("  TOP ARTISTS (by track count)")
        max_cnt = max(cnt for _, cnt in d["top_artists"]) or 1
        for artist, cnt in d["top_artists"]:
            bar = _bar(cnt, max_cnt, 24)
            disp = artist if len(artist) <= 28 else artist[:25] + "..."
            print(f"  {disp:<28} {cnt:>6,}  {bar}")
        print()

    # ── Recent activity (7 days) ──────────────────────────────────────────────
    if d["recent_events"]:
        print("  RECENT ACTIVITY (last 7 days)")
        for event_type, cnt, last_ts in d["recent_events"]:
            ts_short = (last_ts or "")[:16]
            print(f"  {event_type:<28} {cnt:>6,}   last: {ts_short}")
        print()

    print(sep)
    print()


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MUSAEUS dashboard report — library stats at a glance."
    )
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    parser.add_argument("--wide", action="store_true", help="Use wider terminal columns (100)")
    args = parser.parse_args()

    try:
        cfg = get_config()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    conn = open_db(cfg.db_path)
    try:
        data = _gather(conn)
    finally:
        conn.close()

    if args.json:
        # Exclude non-serialisable keys or convert to plain dicts
        out = dict(data.items())
        print(json.dumps(out, indent=2, default=str))
    else:
        _print_report(data, cfg, wide=args.wide)


if __name__ == "__main__":
    main()
