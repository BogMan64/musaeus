#!/usr/bin/env python3
"""
MUSAEUS — Upgrade Checker
Find MP3/AAC tracks where a lossless version of the same song exists elsewhere
in the archive.  Outputs a prioritised upgrade list.

What it does:
  - Loads all CATALOGUED tracks grouped by artist + normalised title
  - Within each group, checks if any lossless version (FLAC/ALAC/WAV) exists
    alongside a lossy version (MP3/AAC/OGG)
  - Reports each such group as an "upgrade available" candidate
  - Outputs a text report + optional CSV to RUNS_ROOT/upgrade_check_report.txt
  - Also flags tracks where a lossless exists at a higher bitrate than the
    current best known version

Usage:
    python3 scripts/musaeus_upgrade_check.py
    python3 scripts/musaeus_upgrade_check.py --csv          # also write CSV
    python3 scripts/musaeus_upgrade_check.py --min-gap 100  # kbps gap threshold

ORPHEUS equivalent: SCRIPTS/orpheus_upgrade_checker.py
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.config import LOSSLESS_EXTENSIONS, LOSSY_EXTENSIONS, get_config
from musaeus.db import open_db

# ── Normalisation ─────────────────────────────────────────────────────────────

_ARTICLE_RE = re.compile(
    r"^(the|a|an|le|la|les|el|los|de|het|een|die|das|ein|eine)\s+",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[^\w\s]")


def _norm(s: str) -> str:
    """Normalise a string for fuzzy comparison: lower, strip articles/punct."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower().strip()
    s = _ARTICLE_RE.sub("", s)
    s = _PUNCT_RE.sub("", s)
    return " ".join(s.split())


# ── Data gathering ────────────────────────────────────────────────────────────


def _gather(conn) -> list[dict]:  # type: ignore[type-arg]
    rows = conn.execute(
        """
        SELECT file_path, artist, title, bitrate, codec
        FROM archive
        WHERE status = 'CATALOGUED'
          AND artist IS NOT NULL AND trim(artist) != ''
          AND title  IS NOT NULL AND trim(title)  != ''
        ORDER BY artist, title
        """
    ).fetchall()
    return [dict(r) for r in rows]


# ── Analysis ──────────────────────────────────────────────────────────────────


def _analyse(rows: list[dict]) -> list[dict]:
    """
    Group tracks by (norm_artist, norm_title).
    Return upgrade candidates where lossy + lossless both exist.
    """
    # group_key → list of rows
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        key = (_norm(row["artist"]), _norm(row["title"]))
        groups[key].append(row)

    candidates = []
    for (norm_artist, norm_title), members in groups.items():
        if len(members) < 2:
            continue

        lossless = [
            m for m in members if Path(m["file_path"]).suffix.lower() in LOSSLESS_EXTENSIONS
        ]
        lossy = [m for m in members if Path(m["file_path"]).suffix.lower() in LOSSY_EXTENSIONS]

        if not (lossless and lossy):
            continue

        # Best lossless bitrate
        best_lossless_br = max((int(m["bitrate"] or 0) for m in lossless), default=0)
        # Best lossy bitrate
        best_lossy_br = max((int(m["bitrate"] or 0) for m in lossy), default=0)

        candidates.append(
            {
                "norm_artist": norm_artist,
                "norm_title": norm_title,
                "display_artist": members[0]["artist"],
                "display_title": members[0]["title"],
                "lossless_paths": [m["file_path"] for m in lossless],
                "lossy_paths": [m["file_path"] for m in lossy],
                "best_lossless_br": best_lossless_br,
                "best_lossy_br": best_lossy_br,
                "total_copies": len(members),
            }
        )

    # Sort by artist then title
    candidates.sort(key=lambda x: (x["norm_artist"], x["norm_title"]))
    return candidates


# ── Report rendering ──────────────────────────────────────────────────────────


def _print_report(candidates: list[dict], cfg, write_csv: bool, min_gap: int) -> None:
    # Filter by min_gap if set
    if min_gap > 0:
        candidates = [
            c
            for c in candidates
            if (c["best_lossless_br"] - c["best_lossy_br"]) >= min_gap or c["best_lossless_br"] == 0
        ]

    runs_root = cfg.runs_root
    runs_root.mkdir(parents=True, exist_ok=True)
    report_path = runs_root / "upgrade_check_report.txt"

    lines = [
        "MUSAEUS UPGRADE CHECK REPORT",
        f"Vault  : {cfg.vault_root}",
        f"Found  : {len(candidates)} upgrade candidate(s)",
        "=" * 72,
        "",
    ]

    for c in candidates:
        lines.append(f"  {c['display_artist']} — {c['display_title']}")
        lines.append(
            f"    Lossless: {c['best_lossless_br']:,} kbps  ({len(c['lossless_paths'])} file(s))"
        )
        for p in c["lossless_paths"]:
            lines.append(f"      ✓  {p}")
        lines.append(f"    Lossy  : {c['best_lossy_br']:,} kbps  ({len(c['lossy_paths'])} file(s))")
        for p in c["lossy_paths"]:
            lines.append(f"      ✗  {p}")
        lines.append("")

    report_text = "\n".join(lines)
    print(report_text)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    print(f"\nReport written to: {report_path}")

    if write_csv:
        csv_path = runs_root / "upgrade_check_report.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "artist",
                    "title",
                    "lossless_bitrate",
                    "lossy_bitrate",
                    "lossless_path",
                    "lossy_path",
                ],
            )
            w.writeheader()
            for c in candidates:
                # One row per lossy file
                for lp in c["lossy_paths"]:
                    w.writerow(
                        {
                            "artist": c["display_artist"],
                            "title": c["display_title"],
                            "lossless_bitrate": c["best_lossless_br"],
                            "lossy_bitrate": c["best_lossy_br"],
                            "lossless_path": c["lossless_paths"][0],
                            "lossy_path": lp,
                        }
                    )
        print(f"CSV written to   : {csv_path}")


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MUSAEUS upgrade checker — find lossy tracks with lossless versions."
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Also write a CSV of upgrade candidates",
    )
    parser.add_argument(
        "--min-gap",
        type=int,
        default=0,
        metavar="KBPS",
        help="Only report if lossless bitrate exceeds lossy by at least N kbps "
        "(default: 0 = report all)",
    )
    args = parser.parse_args()

    try:
        cfg = get_config()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    conn = open_db(cfg.db_path)
    try:
        rows = _gather(conn)
    finally:
        conn.close()

    candidates = _analyse(rows)
    _print_report(candidates, cfg, write_csv=args.csv, min_gap=args.min_gap)

    sys.exit(0 if not candidates else 0)


if __name__ == "__main__":
    main()
