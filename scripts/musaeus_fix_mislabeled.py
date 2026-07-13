#!/usr/bin/env python3
"""
MUSAEUS — Fix Mislabeled Audio Files

Finds duplicate pairs where the filename doesn't match the embedded TITLE tag,
indicating a mislabeled file (audio content ≠ what the filename claims).

Logic:
  1. Query all pending duplicate groups from musaeus.db
  2. Probe each file's embedded TITLE/ARTIST via ffprobe
  3. For each group:
     - If filename matches embedded title → naming variant only
     - If filename differs from embedded title → one file is mislabeled
  4. Rename mislabeled files using embedded tags
  5. For remaining true duplicates (same audio, same correct name), keep
     the highest quality (largest file) and quarantine the rest.

Usage:
  python3 musaeus_fix_mislabeled.py [--apply] [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

MUSAEUS_VAULT = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT")
DEFAULT_DB = MUSAEUS_VAULT / "musaeus.db"
QUARANTINE = MUSAEUS_VAULT / "QUARANTINE"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _probe_tags(path: Path) -> dict[str, str]:
    """Return {title, artist} from embedded tags via ffprobe. Returns {} on failure."""
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_entries",
        "format_tags=title,artist,TITLE,ARTIST",
        str(path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        data = json.loads(r.stdout)
        tags = data.get("format", {}).get("tags", {})
        # ffprobe returns keys in original case; normalise
        return {
            "title": tags.get("title") or tags.get("TITLE") or "",
            "artist": tags.get("artist") or tags.get("ARTIST") or "",
        }
    except Exception:
        return {}


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation and articles for fuzzy comparison."""
    text = text.lower()
    text = re.sub(r"\b(the|a|an)\b", "", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _stem(path: Path) -> str:
    """File stem without leading artist prefix 'Artist - '."""
    stem = path.stem
    if " - " in stem:
        stem = stem.split(" - ", 1)[1]
    return stem


def _is_mislabeled(path: Path, embedded_title: str) -> bool:
    """Return True if the filename clearly doesn't match the embedded title."""
    if not embedded_title:
        return False
    fn_norm = _normalise(_stem(path))
    tag_norm = _normalise(embedded_title)
    if not fn_norm or not tag_norm:
        return False
    # Simple word overlap: if fewer than 25% of words match → mislabeled
    fn_words = set(fn_norm.split())
    tag_words = set(tag_norm.split())
    if not fn_words or not tag_words:
        return False
    overlap = fn_words & tag_words
    ratio = len(overlap) / max(len(fn_words), len(tag_words))
    return ratio < 0.25


def _safe_filename(artist: str, title: str, ext: str) -> str:
    """Build a clean Artist - Title.ext filename from embedded tags."""

    def _clean(s: str) -> str:
        return re.sub(r'[<>:"/\\|?*]', "", s).strip()

    if artist and title:
        return f"{_clean(artist)} - {_clean(title)}{ext}"
    elif title:
        return f"{_clean(title)}{ext}"
    return ""


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(description="Fix mislabeled MUSAEUS audio files")
    ap.add_argument(
        "--apply", action="store_true", help="Apply renames and quarantine (default: dry run)"
    )
    ap.add_argument("--db", default=str(DEFAULT_DB), help="Path to musaeus.db")
    args = ap.parse_args()

    dry_run = not args.apply
    db_path = Path(args.db)

    if not db_path.exists():
        print(f"DB not found: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Fetch all pending duplicate groups
    groups: dict[str, list[str]] = {}
    rows = conn.execute(
        "SELECT group_id, file_path FROM duplicates WHERE status='pending' ORDER BY group_id, file_path"
    ).fetchall()
    for row in rows:
        gid = row["group_id"]
        groups.setdefault(gid, []).append(row["file_path"])

    print(
        f"\n── MUSAEUS Fix Mislabeled {'[DRY RUN]' if dry_run else '[LIVE]'} ──────────────────────"
    )
    print(f"  DB       : {db_path}")
    print(f"  Groups   : {len(groups)} pending duplicate groups\n")

    mislabeled_count = 0
    quarantine_count = 0

    for group_id, file_paths in sorted(groups.items()):
        # Probe embedded tags for all files in group
        probe: dict[str, dict] = {}
        for fp in file_paths:
            path = Path(fp)
            if path.exists():
                probe[fp] = _probe_tags(path)
            else:
                probe[fp] = {}

        # Determine which files are mislabeled
        mislabeled: list[str] = []
        correct: list[str] = []
        for fp, tags in probe.items():
            if _is_mislabeled(Path(fp), tags.get("title", "")):
                mislabeled.append(fp)
            else:
                correct.append(fp)

        if mislabeled:
            print(f"  GROUP {group_id}  — {len(mislabeled)} mislabeled:")
            for fp in mislabeled:
                tags = probe[fp]
                embedded_title = tags.get("title", "(no title tag)")
                embedded_artist = tags.get("artist", "")
                path = Path(fp)
                new_name = _safe_filename(embedded_artist, embedded_title, path.suffix)
                print(f"    RENAME: {path.name}")
                print(f"      → {new_name}  (embedded title: {embedded_title!r})")
                if not dry_run and new_name:
                    new_path = path.parent / new_name
                    if not new_path.exists():
                        path.rename(new_path)
                        conn.execute(
                            "UPDATE archive SET file_path=? WHERE file_path=?",
                            (str(new_path), fp),
                        )
                        conn.execute(
                            "UPDATE duplicates SET file_path=? WHERE file_path=?",
                            (str(new_path), fp),
                        )
                        print("      ✓ renamed")
                    else:
                        print("      ⚠ target exists, skipped rename")
            mislabeled_count += len(mislabeled)

        # After mislabeled fix: if group still has multiple files, keep highest quality
        # (largest file = better quality / lossless). Deduplicate paths first.
        seen_paths: set[str] = set()
        all_existing: list[str] = []
        for fp in file_paths:
            if fp not in seen_paths and Path(fp).exists():
                seen_paths.add(fp)
                all_existing.append(fp)

        if len(all_existing) > 1:
            by_size = sorted(all_existing, key=lambda fp: Path(fp).stat().st_size, reverse=True)
            keeper = by_size[0]
            losers = by_size[1:]
            if losers:
                print(f"  DEDUPE  {group_id}  — keep: {Path(keeper).name}")
                for fp in losers:
                    p = Path(fp)
                    if not p.exists():
                        continue  # already moved earlier in this run
                    size_kb = p.stat().st_size // 1024
                    print(f"    QUARANTINE: {p.name}  ({size_kb:,} KB)")
                    if not dry_run:
                        QUARANTINE.mkdir(parents=True, exist_ok=True)
                        dest = QUARANTINE / p.name
                        if dest.exists():
                            dest = QUARANTINE / (p.stem + "_dup" + p.suffix)
                        p.rename(dest)
                        conn.execute(
                            "UPDATE archive SET status='QUARANTINED' WHERE file_path=?",
                            (fp,),
                        )
                        conn.execute(
                            "UPDATE duplicates SET status='resolved' WHERE file_path=?",
                            (fp,),
                        )
                        print(f"      ✓ quarantined → {dest.name}")
                        quarantine_count += 1

    if not dry_run:
        conn.commit()

    conn.close()

    print(f"\n  Mislabeled renamed : {mislabeled_count}")
    print(f"  Duplicates quarantined: {quarantine_count}")
    if dry_run:
        print("\n  Run with --apply to make changes.")


if __name__ == "__main__":
    main()
