#!/usr/bin/env python3
"""
MUSAEUS — Album & Artist Review/Approval Workflow

Generates TSV approval sheets from uncertain/changed metadata in the archive,
then applies human-approved fixes back to the DB and file tags.

Workflow:
  1. `musaeus review generate`  — Scan archive for fixable issues, write TSV sheets
  2. Human edits the TSV        — Mark approve=yes or approve=no per row
  3. `musaeus review apply`     — Read approved rows and update DB + file tags

TSV sheets live in: <vault>/MetaData/review/
  - artist_review.tsv    — Artist name corrections (misspellings, variants, canon)
  - album_review.tsv     — Album name / year / genre corrections
"""

from __future__ import annotations

import csv
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .preview_guard import reject_legacy_preview

logger = logging.getLogger(__name__)


@dataclass
class ReviewEntry:
    """One row in a review sheet."""

    file_path: str
    field_name: str
    current_value: str
    suggested_value: str
    source: str  # e.g. "canon_fuzzy", "lastfm", "manual"
    confidence: float = 0.0
    approve: str = ""  # blank = pending, "yes" = approved, "no" = rejected
    notes: str = ""


@dataclass
class ReviewReport:
    """Summary of a generate or apply run."""

    generated: int = 0
    approved: int = 0
    rejected: int = 0
    pending: int = 0
    applied: int = 0
    errors: list[str] = field(default_factory=list)


# ── TSV I/O ───────────────────────────────────────────────────────────────────

_FIELDS = [
    "approve",
    "file_path",
    "field_name",
    "current_value",
    "suggested_value",
    "source",
    "confidence",
    "notes",
]


def write_review_tsv(path: Path, entries: list[ReviewEntry]) -> int:
    """Write review entries to a TSV file. Returns count written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_FIELDS, delimiter="\t")
        writer.writeheader()
        for e in entries:
            writer.writerow(
                {
                    "approve": e.approve,
                    "file_path": e.file_path,
                    "field_name": e.field_name,
                    "current_value": e.current_value,
                    "suggested_value": e.suggested_value,
                    "source": e.source,
                    "confidence": f"{e.confidence:.2f}",
                    "notes": e.notes,
                }
            )
    return len(entries)


def read_review_tsv(path: Path) -> list[ReviewEntry]:
    """Read review entries from a TSV file."""
    if not path.exists():
        return []
    entries: list[ReviewEntry] = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="	")
        for row in reader:
            conf_str = row.get("confidence", "0").strip()
            try:
                confidence = float(conf_str) if conf_str else 0.0
            except ValueError:
                confidence = 0.0
                logger.warning(
                    "Invalid confidence value '%s' in %s, defaulting to 0.0", conf_str, path
                )
            entries.append(
                ReviewEntry(
                    file_path=row.get("file_path", ""),
                    field_name=row.get("field_name", ""),
                    current_value=row.get("current_value", ""),
                    suggested_value=row.get("suggested_value", ""),
                    source=row.get("source", ""),
                    confidence=confidence,
                    approve=row.get("approve", "").strip().lower(),
                    notes=row.get("notes", ""),
                )
            )
    return entries


# ── Generate review sheets ────────────────────────────────────────────────────


def generate_artist_review(
    conn: sqlite3.Connection,
    canon_path: Path,
    output_path: Path,
) -> ReviewReport:
    """
    Generate artist_review.tsv from archive entries whose artist differs
    from the canonical form.

    Sources of suggestions:
      - ArtistCanon fuzzy match (confidence from match score)
      - Duplicate entries with variant spellings
    """
    report = ReviewReport()

    # Load canon
    from .canon.artist import ArtistCanon

    canon = ArtistCanon(canon_path)

    # Find all unique artists in archive
    rows = conn.execute(
        "SELECT DISTINCT artist, file_path FROM archive "
        "WHERE artist IS NOT NULL AND artist != '' AND status != 'GHOST'"
    ).fetchall()

    entries: list[ReviewEntry] = []
    seen: set[str] = set()

    for row in rows:
        raw_artist = row["artist"]
        file_path = row["file_path"]

        # Skip if already reviewed this artist
        norm_key = raw_artist.strip().lower()
        if norm_key in seen:
            continue

        resolved = canon.resolve(raw_artist)
        # Only flag if canon resolved differently (and it's not just returning raw)
        if resolved != raw_artist and resolved != raw_artist.strip():
            entries.append(
                ReviewEntry(
                    file_path=file_path,
                    field_name="artist",
                    current_value=raw_artist,
                    suggested_value=resolved,
                    source="canon_fuzzy",
                    confidence=0.88,
                )
            )
            seen.add(norm_key)

    report.generated = len(entries)
    if entries:
        write_review_tsv(output_path, entries)
        logger.info("Wrote %d artist review entries to %s", len(entries), output_path)

    return report


def generate_album_review(
    conn: sqlite3.Connection,
    output_path: Path,
) -> ReviewReport:
    """
    Generate album_review.tsv from archive entries with quality issues:
      - Missing year
      - Missing genre
      - Album name inconsistencies within same artist
    """
    report = ReviewReport()
    entries: list[ReviewEntry] = []

    # Find albums with missing year
    rows = conn.execute(
        "SELECT file_path, artist, album, year, genre FROM archive "
        "WHERE status != 'GHOST' AND (year IS NULL OR year = '' OR genre IS NULL OR genre = '')"
    ).fetchall()

    for row in rows:
        if not row["year"]:
            entries.append(
                ReviewEntry(
                    file_path=row["file_path"],
                    field_name="year",
                    current_value="",
                    suggested_value="",
                    source="missing_metadata",
                    confidence=0.0,
                    notes=f"Album: {row['album'] or '?'}, Artist: {row['artist'] or '?'}",
                )
            )
        if not row["genre"]:
            entries.append(
                ReviewEntry(
                    file_path=row["file_path"],
                    field_name="genre",
                    current_value="",
                    suggested_value="",
                    source="missing_metadata",
                    confidence=0.0,
                    notes=f"Album: {row['album'] or '?'}, Artist: {row['artist'] or '?'}",
                )
            )

    # Find album name inconsistencies (same artist, similar album names)
    artist_albums = conn.execute(
        "SELECT DISTINCT artist, album FROM archive "
        "WHERE status != 'GHOST' AND artist IS NOT NULL AND album IS NOT NULL "
        "ORDER BY artist, album"
    ).fetchall()

    # Group by artist and check for near-duplicate album names
    from collections import defaultdict

    by_artist: dict[str, list[str]] = defaultdict(list)
    for row in artist_albums:
        by_artist[row["artist"]].append(row["album"])

    for artist, albums in by_artist.items():
        if len(albums) < 2:
            continue
        # Simple check: same album with different casing or minor diff
        norm_map: dict[str, list[str]] = defaultdict(list)
        for album in albums:
            norm_map[album.strip().lower()].append(album)
        for _norm, variants in norm_map.items():
            if len(variants) > 1:
                canonical = sorted(variants, key=len)[-1]  # longest = most complete
                for v in variants:
                    if v != canonical:
                        # Find a file with this album
                        sample = conn.execute(
                            "SELECT file_path FROM archive WHERE artist=? AND album=? LIMIT 1",
                            (artist, v),
                        ).fetchone()
                        if sample:
                            entries.append(
                                ReviewEntry(
                                    file_path=sample["file_path"],
                                    field_name="album",
                                    current_value=v,
                                    suggested_value=canonical,
                                    source="album_variant",
                                    confidence=0.90,
                                    notes=f"Artist: {artist}",
                                )
                            )

    report.generated = len(entries)
    if entries:
        write_review_tsv(output_path, entries)
        logger.info("Wrote %d album review entries to %s", len(entries), output_path)

    return report


# ── Apply approved fixes ──────────────────────────────────────────────────────


def apply_approved_fixes(
    conn: sqlite3.Connection,
    review_path: Path,
    run_id: str,
    *,
    dry_run: bool = False,
) -> ReviewReport:
    """
    Read a review TSV, apply all rows marked approve=yes to the archive DB.
    Returns a summary report.
    """
    from .db import log_event

    report = ReviewReport()
    entries = read_review_tsv(review_path)

    for entry in entries:
        if entry.approve == "yes":
            report.approved += 1
            if not dry_run:
                # Update archive
                conn.execute(
                    f"UPDATE archive SET {entry.field_name} = ? WHERE file_path = ?",
                    (entry.suggested_value, entry.file_path),
                )
                log_event(
                    conn,
                    run_id,
                    "REVIEW_APPLIED",
                    file_path=entry.file_path,
                    old_value=entry.current_value,
                    new_value=entry.suggested_value,
                    stage="approval",
                    note=f"{entry.field_name} ({entry.source})",
                )
                report.applied += 1
        elif entry.approve == "no":
            report.rejected += 1
        else:
            report.pending += 1

    if not dry_run and report.applied > 0:
        conn.commit()

    return report


# ── CLI entry points ──────────────────────────────────────────────────────────


def cmd_review_generate(dry_run: bool = False) -> int:
    """Generate review sheets from current archive state."""
    if dry_run:
        return reject_legacy_preview()

    import sys

    from .config import get_config
    from .db import open_db

    try:
        cfg = get_config()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    review_dir = cfg.meta_dir / "review"
    review_dir.mkdir(parents=True, exist_ok=True)

    conn = open_db(cfg.db_path)
    try:
        mode = " [DRY RUN]" if dry_run else ""
        print(f"\nMusaeus review generate{mode}")
        print(f"  Output: {review_dir}")
        print()

        # Artist review
        artist_path = review_dir / "artist_review.tsv"
        canon_path = cfg.meta_dir / "artist_canon.tsv"
        artist_report = generate_artist_review(conn, canon_path, artist_path)
        print(f"  Artist review : {artist_report.generated} entries")
        if artist_report.generated > 0:
            print(f"    → {artist_path}")

        # Album review
        album_path = review_dir / "album_review.tsv"
        album_report = generate_album_review(conn, album_path)
        print(f"  Album review  : {album_report.generated} entries")
        if album_report.generated > 0:
            print(f"    → {album_path}")

        total = artist_report.generated + album_report.generated
        if total == 0:
            print("\n  ✓ No review items found — library metadata is clean.")
        else:
            print(f"\n  Total: {total} entries generated for review.")
            print("  Edit the TSV files, set approve=yes or approve=no, then run:")
            print("    musaeus review apply")

        print()
        return 0
    finally:
        conn.close()


def cmd_review_apply(dry_run: bool = False) -> int:
    """Apply approved fixes from review sheets."""
    if dry_run:
        return reject_legacy_preview()

    import sys

    from .config import get_config
    from .context import RunContext
    from .db import open_db

    try:
        cfg = get_config()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    review_dir = cfg.meta_dir / "review"
    conn = open_db(cfg.db_path)

    try:
        ctx = RunContext.new(cfg, conn, dry_run=dry_run)
        mode = " [DRY RUN]" if dry_run else ""
        print(f"\nMusaeus review apply{mode}")
        print(f"  Source: {review_dir}")
        print()

        total_applied = 0
        total_approved = 0
        total_rejected = 0
        total_pending = 0

        for tsv_name in ("artist_review.tsv", "album_review.tsv"):
            tsv_path = review_dir / tsv_name
            if not tsv_path.exists():
                print(f"  {tsv_name}: not found (skipped)")
                continue

            report = apply_approved_fixes(conn, tsv_path, ctx.run_id, dry_run=dry_run)
            print(f"  {tsv_name}:")
            print(f"    Approved : {report.approved}")
            print(f"    Rejected : {report.rejected}")
            print(f"    Pending  : {report.pending}")
            if not dry_run:
                print(f"    Applied  : {report.applied}")

            total_applied += report.applied
            total_approved += report.approved
            total_rejected += report.rejected
            total_pending += report.pending

        print()
        if total_approved == 0:
            print("  No approved entries found. Edit the TSV and set approve=yes.")
        elif dry_run:
            print(f"  Would apply {total_approved} fix(es).")
        else:
            print(f"  ✓ Applied {total_applied} fix(es) to archive.")
            if total_pending > 0:
                print(f"    {total_pending} entries still pending review.")

        ctx.finish()
        print()
        return 0
    finally:
        conn.close()


def cmd_review_status() -> int:
    """Show status of pending review sheets."""
    import sys

    from .config import get_config

    try:
        cfg = get_config()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    review_dir = cfg.meta_dir / "review"
    print("\nMusaeus Review Status")
    print(f"  Review dir: {review_dir}")
    print()

    if not review_dir.exists():
        print("  No review sheets found. Run: musaeus review generate")
        print()
        return 0

    for tsv_name in ("artist_review.tsv", "album_review.tsv"):
        tsv_path = review_dir / tsv_name
        if not tsv_path.exists():
            print(f"  {tsv_name}: not found")
            continue

        entries = read_review_tsv(tsv_path)
        approved = sum(1 for e in entries if e.approve == "yes")
        rejected = sum(1 for e in entries if e.approve == "no")
        pending = sum(1 for e in entries if e.approve not in ("yes", "no"))

        print(f"  {tsv_name}: {len(entries)} entries")
        print(f"    ✓ approved={approved}  ✗ rejected={rejected}  ○ pending={pending}")

    print()
    return 0
