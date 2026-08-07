#!/usr/bin/env python3
"""
MUSAEUS — Canon Review
Build suspicious-entry reports for artist/genre/album canons, and apply
human-approved fix CSVs back to the archive DB.

What it does:
  MODE: report
    - Loads the genre canon from MetaData/Genre_Canonical_Map.txt
    - Finds archive genres NOT in the canon (unrecognised genres)
    - Finds archive artists with name variants (case/punct differences)
    - Finds albums with suspicious names (all-caps, very short, numbers only)
    - Writes canon_review_report.txt + canon_review.csv for human review

  MODE: apply
    - Reads a CSV (canon_fixes.csv) with columns:
        file_path, field, old_value, new_value, approved
    - Applies rows where approved == 'yes' to the archive DB
    - Logs CANON_FIX event per change
    - Writes canon_apply_report.txt

CSV format for apply mode:
    file_path,field,old_value,new_value,approved
    /vault/Artist/Album/track.flac,genre,Rnb,R&B,yes
    /vault/Artist/Album/track.flac,artist,Radiohead ,Radiohead,yes

Usage:
    python3 scripts/musaeus_canon_review.py report
    python3 scripts/musaeus_canon_review.py report --csv canon_review.csv
    python3 scripts/musaeus_canon_review.py apply --fixes canon_fixes.csv
    python3 scripts/musaeus_canon_review.py apply --fixes canon_fixes.csv --dry-run
        # temporarily unavailable: exits with the safety block

ORPHEUS equivalents: SCRIPTS/generate_genre_canon.py,
                     SCRIPTS/generate_album_canon.py,
                     SCRIPTS/apply_approved_genre_fixes.py,
                     SCRIPTS/apply_approved_review_fixes.py,
                     SCRIPTS/review_genre_canon_suspicious.py
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

from musaeus.config import get_config
from musaeus.db import open_db
from musaeus.preview_guard import LEGACY_PREVIEW_HELP, reject_legacy_preview

# ── Helpers ───────────────────────────────────────────────────────────────────

_PUNCT_RE = re.compile(r"[^\w\s]")


def _norm(s: str) -> str:
    """Normalise for comparison: lower, strip accents/punct."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return _PUNCT_RE.sub("", s).lower().strip()


def _load_genre_canon(meta_dir: Path) -> set[str]:
    """Load Genre_Canonical_Map.txt — return the set of canonical genre names."""
    canon_path = meta_dir / "Genre_Canonical_Map.txt"
    genres: set[str] = set()
    if not canon_path.exists():
        return genres
    with open(canon_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Format: alias -> canonical  OR  canonical
            canonical = line.split("->")[-1].strip() if "->" in line else line
            genres.add(canonical.lower())
    return genres


# ── Report mode ───────────────────────────────────────────────────────────────


def _report(conn, cfg, csv_path: Path | None) -> None:
    rows = conn.execute(
        """
        SELECT file_path, artist, album, title, genre
        FROM archive
        WHERE status = 'CATALOGUED'
          AND artist IS NOT NULL
        ORDER BY artist, album
        """
    ).fetchall()

    genre_canon = _load_genre_canon(cfg.meta_dir)

    # Unrecognised genres
    unknown_genres: dict[str, int] = defaultdict(int)
    for row in rows:
        g = (row["genre"] or "").strip()
        if g and g.lower() not in genre_canon:
            unknown_genres[g] += 1

    # Artist name variants (same normalised form, different raw forms)
    artist_variants: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        a = (row["artist"] or "").strip()
        if a:
            artist_variants[_norm(a)].add(a)
    variant_groups = {k: v for k, v in artist_variants.items() if len(v) > 1}

    # Suspicious album names
    suspicious_albums: list[tuple[str, str, str]] = []
    seen_albums: set[str] = set()
    for row in rows:
        album = (row["album"] or "").strip()
        artist = (row["artist"] or "").strip()
        key = f"{artist}||{album}"
        if key in seen_albums or not album:
            continue
        seen_albums.add(key)
        if album.isupper() and len(album) > 3:
            suspicious_albums.append((artist, album, "ALL_CAPS"))
        elif re.match(r"^[\d\s\-_.]+$", album):
            suspicious_albums.append((artist, album, "NUMBERS_ONLY"))
        elif len(album) <= 2:
            suspicious_albums.append((artist, album, "TOO_SHORT"))
        elif album.startswith("Track ") or album.startswith("track "):
            suspicious_albums.append((artist, album, "GENERIC_TRACK_NAME"))

    # Print report
    runs_root = cfg.runs_root
    runs_root.mkdir(parents=True, exist_ok=True)
    report_path = runs_root / "canon_review_report.txt"

    lines = [
        "MUSAEUS CANON REVIEW REPORT",
        f"Vault  : {cfg.vault_root}",
        "=" * 72,
        "",
        f"UNRECOGNISED GENRES ({len(unknown_genres)}):",
    ]
    for genre, count in sorted(unknown_genres.items(), key=lambda x: -x[1]):
        lines.append(f"  {genre:<30}  ({count} tracks)")
    lines.append("")

    lines.append(f"ARTIST NAME VARIANTS ({len(variant_groups)} groups):")
    for norm_key, variants in sorted(variant_groups.items()):
        lines.append(f"  Normalised: '{norm_key}'")
        for v in sorted(variants):
            lines.append(f"    → '{v}'")
    lines.append("")

    lines.append(f"SUSPICIOUS ALBUMS ({len(suspicious_albums)}):")
    for artist, album, reason in suspicious_albums:
        lines.append(f"  [{reason}] {artist} — {album}")
    lines.append("")

    report_text = "\n".join(lines)
    print(report_text)
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report_text)
    print(f"\nReport written to: {report_path}")

    # Optional CSV for human review
    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["type", "artist", "album_or_genre", "variant", "count", "note"])
            for genre, count in sorted(unknown_genres.items()):
                w.writerow(["UNKNOWN_GENRE", "", genre, "", count, ""])
            for norm_key, variants in sorted(variant_groups.items()):
                for v in sorted(variants):
                    w.writerow(["ARTIST_VARIANT", v, "", norm_key, "", ""])
            for artist, album, reason in suspicious_albums:
                w.writerow(["SUSPICIOUS_ALBUM", artist, album, "", "", reason])
        print(f"CSV written to   : {csv_path}")


# ── Apply mode ────────────────────────────────────────────────────────────────


def _apply(conn, cfg, fixes_path: Path, dry_run: bool) -> None:
    if not fixes_path.exists():
        print(f"ERROR: fixes file not found: {fixes_path}", file=sys.stderr)
        sys.exit(1)

    with open(fixes_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fixes = list(reader)

    approved = [r for r in fixes if (r.get("approved") or "").strip().lower() == "yes"]
    print(f"Loaded {len(fixes)} rows, {len(approved)} approved.")

    ALLOWED_FIELDS = {"genre", "artist", "album", "title", "year"}
    applied = 0
    skipped = 0

    runs_root = cfg.runs_root
    runs_root.mkdir(parents=True, exist_ok=True)
    report_path = runs_root / "canon_apply_report.txt"
    report_lines = [
        "MUSAEUS CANON APPLY REPORT",
        f"Fixes file : {fixes_path}",
        f"Dry run    : {dry_run}",
        "=" * 72,
        "",
    ]

    for row in approved:
        fp = (row.get("file_path") or "").strip()
        field = (row.get("field") or "").strip().lower()
        new_val = (row.get("new_value") or "").strip()

        if not fp or field not in ALLOWED_FIELDS:
            skipped += 1
            continue

        if not dry_run:
            conn.execute(
                f"UPDATE archive SET {field}=? WHERE file_path=?",
                (new_val, fp),
            )
            report_lines.append(f"  APPLIED  {field}: '{row.get('old_value', '')}' → '{new_val}'")
            report_lines.append(f"           {fp}")
        else:
            report_lines.append(f"  DRY-RUN  {field}: '{row.get('old_value', '')}' → '{new_val}'")
            report_lines.append(f"           {fp}")
        applied += 1

    if not dry_run:
        conn.commit()

    report_lines.append("")
    report_lines.append(f"Applied: {applied}  Skipped: {skipped}")

    report_text = "\n".join(report_lines)
    print(report_text)
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report_text)
    print(f"\nReport written to: {report_path}")


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MUSAEUS canon review — audit canons and apply approved fixes."
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    rep = sub.add_parser("report", help="Generate canon review report")
    rep.add_argument(
        "--csv",
        metavar="PATH",
        help="Also write a CSV for spreadsheet review",
    )

    appl = sub.add_parser("apply", help="Apply approved fixes from CSV")
    appl.add_argument(
        "--fixes",
        required=True,
        metavar="PATH",
        help="CSV file with approved fixes (file_path,field,old_value,new_value,approved)",
    )
    appl.add_argument(
        "--dry-run",
        action="store_true",
        help=LEGACY_PREVIEW_HELP,
    )

    args = parser.parse_args()

    if args.mode == "apply" and args.dry_run:
        sys.exit(reject_legacy_preview())

    try:
        cfg = get_config()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    conn = open_db(cfg.db_path)
    try:
        if args.mode == "report":
            csv_path = Path(args.csv) if getattr(args, "csv", None) else None
            _report(conn, cfg, csv_path)
        elif args.mode == "apply":
            _apply(conn, cfg, Path(args.fixes), dry_run=args.dry_run)
    finally:
        conn.close()

    sys.exit(0)


if __name__ == "__main__":
    main()
