#!/usr/bin/env python3
"""Stage the next N files from Curated.RAW.Files into INBOX, and record it.

Why a manifest exists
---------------------
The first rebuild batches were staged by ad-hoc shell loops that left no
record of which raw files had been consumed. Reconstructing it afterwards was
guesswork: finalize renames files, so a raw basename may match either
archive.filename (the original) or the basename of file_path (the renamed
copy), and neither matching is complete. This script writes the manifest the
earlier batches lacked, so "what is left?" is answered by reading a file
rather than by inference.

The manifest is the record of a decision, not a rebuildable artifact, so it
lives in MetaData/ -- outside Libraries/, per the standing rule.

Copies are written to a .part file, verified by size, then renamed. A 15-min
timeout once killed a bare shutil.copy2 mid-write and left a truncated 4 MB
file that looked finished; existence is not completeness.
"""
from __future__ import annotations

import argparse
import csv
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/mnt/FORGE2TB/Projects/MUSAEUS")
from musaeus.config import MusicConfig

RAW_ROOT = Path("/media/grey/USB1/2.- Curated.RAW.Files")
MANIFEST_NAME = "raw_rebuild_manifest.csv"


# States that represent a settled outcome. A rebuild must never recompute
# these from the archive table: a denied file has no archive row by design,
# so recomputing would mark it pending, it would be staged, refused by the
# LEDGER, and staged again on the next rebuild -- for ever.
TERMINAL = {"denied", "verified-new", "missing"}


def build_manifest(cfg: MusicConfig, manifest: Path) -> list[dict]:
    """One row per raw file, with why we think it is already done.

    The archive query is deliberately unfiltered by status: a row in
    DUPE_REVIEW or QUARANTINED was still genuinely processed, so it counts
    as done. Only the absence of any row means unprocessed.
    """
    import os
    import sqlite3

    db = sqlite3.connect(cfg.db_path, timeout=300)
    original = {r[0] for r in db.execute(
        "SELECT filename FROM archive WHERE filename IS NOT NULL")}
    renamed = {os.path.basename(r[0]) for r in db.execute(
        "SELECT file_path FROM archive WHERE file_path IS NOT NULL")}
    db.close()

    led_path = cfg.vault_root / "_db_backups" / "hash_index.db"
    denied_names: set[str] = set()
    if led_path.is_file():
        led = sqlite3.connect(str(led_path), timeout=300)
        denied_names = {
            os.path.basename(r[0])
            for r in led.execute(
                "SELECT source_path FROM denied_hashes WHERE source_path IS NOT NULL")
        }
        led.close()

    # Preserve any settled outcome from a previous manifest.
    previous: dict[str, dict] = {}
    if manifest.exists():
        previous = {r["basename"]: r for r in load(manifest)}

    rows = []
    for p in sorted(RAW_ROOT.rglob("*.m4a"), key=lambda x: x.name):
        prior = previous.get(p.name)
        if prior and prior["state"] in TERMINAL:
            rows.append({"path": str(p), "basename": p.name,
                         "state": prior["state"], "why": prior["why"]})
            continue
        if p.name in original:
            state, why = "done", "archive.filename matches (original name)"
        elif p.name in denied_names:
            state, why = "denied", "on the deny list; the LEDGER would refuse it"
        elif p.name in renamed:
            state, why = "done", "library basename matches (renamed by finalize)"
        else:
            state, why = "pending", ""
        rows.append({"path": str(p), "basename": p.name, "state": state, "why": why})

    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["path", "basename", "state", "why"])
        w.writeheader()
        w.writerows(rows)
    return rows


def load(manifest: Path) -> list[dict]:
    with manifest.open() as fh:
        return list(csv.DictReader(fh))


def save(manifest: Path, rows: list[dict]) -> None:
    tmp = manifest.with_suffix(".part")
    with tmp.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["path", "basename", "state", "why"])
        w.writeheader()
        w.writerows(rows)
    tmp.replace(manifest)


def copy_verified(src: Path, dest: Path) -> None:
    """Write to .part, verify size, then rename. Never leave a partial file
    at the final name -- it would look finished and be skipped for ever."""
    part = dest.with_suffix(dest.suffix + ".part")
    shutil.copy2(src, part)
    if part.stat().st_size != src.stat().st_size:
        part.unlink(missing_ok=True)
        raise OSError(f"short copy: {src.name}")
    part.replace(dest)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=0, help="how many to stage (0 = all pending)")
    ap.add_argument("--rebuild-manifest", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    cfg = MusicConfig.from_env()
    manifest = cfg.meta_dir / MANIFEST_NAME

    if args.rebuild_manifest or not manifest.exists():
        rows = build_manifest(cfg, manifest)
        print(f"manifest built: {manifest}")
    else:
        rows = load(manifest)

    from collections import Counter
    counts = Counter(r["state"] for r in rows)
    print(f"  total {len(rows):,}  " + "  ".join(f"{k} {v:,}" for k, v in sorted(counts.items())))
    if args.status:
        return 0

    pending = [r for r in rows if r["state"] == "pending"]
    take = pending if args.count <= 0 else pending[: args.count]
    if not take:
        print("  nothing pending")
        return 0

    cfg.inbox.mkdir(parents=True, exist_ok=True)
    staged = failed = 0
    for r in take:
        src = Path(r["path"])
        dest = cfg.inbox / src.name
        try:
            if not src.is_file():
                r["state"], r["why"] = "missing", "not on disk at stage time"
                failed += 1
                continue
            copy_verified(src, dest)
            r["state"], r["why"] = "staged", ""
            staged += 1
        except Exception as exc:  # noqa: BLE001
            r["state"], r["why"] = "error", str(exc)[:120]
            failed += 1
        if staged % 200 == 0 and staged:
            save(manifest, rows)
    save(manifest, rows)
    print(f"  staged {staged:,}  failed {failed}  -> {cfg.inbox}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
