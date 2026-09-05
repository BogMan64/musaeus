#!/usr/bin/env python3
"""
MUSAEUS — Migrate existing ALAC-Library content into ALAC_Archive (2026-08-18)

Context: Grey's Phase 1/2A/2B split (distinct from the code's own
Act 1/2/3 pipeline-execution grouping -- see scope doc §4.15) introduces
a new pristine, unbaked tier, ALAC_Archive, sitting ahead of ALAC-Library
in the pipeline: Phase 1 (intake/standardize/dedupe/canonicalize/
finalize) will land new work in ALAC_Archive going forward; Phase 2A
reads from ALAC_Archive and bakes -18 LUFS into ALAC-Library; Phase 2B
reads from ALAC_Archive and bakes -14 LUFS (+ optional masking) into
AAC-Car. Decided explicitly: prospective only -- existing ALAC-Library
content stays where it is until Phase 1 (tonight's other pipeline work)
is confirmed settled, then gets moved with this script.

Why a direct move-and-relink script, not an INBOX re-ingest: hash_index.db
already has every one of these files' audio_hash recorded from their
original Finalize. Dropping them back into INBOX would make CrossDupeStage
recognize them as already-in-the-library and DupeResolverStage would
quarantine them into DUPES_MOVED_FOR_REVIEW instead of landing them in
ALAC_Archive -- the opposite of the goal. This script sidesteps dedup
entirely, matching musaeus_relink_recovered_dupes.py's own precedent for
exactly this class of problem (a 2026-08-14 incident script, same
direct-move-plus-DB-update shape).

Scope, deliberately narrow: moves only the audio files + updates
archive.file_path. Does NOT touch hash_index.db, TuneMyMusic.csv, or
DUPES_MOVED_FOR_REVIEW -- those staying associated with ALAC-Library vs.
moving to live under ALAC_Archive is a separate, not-yet-made decision
(cross-batch dedup should probably key off ALAC_Archive going forward,
since audio_hash is computed on pristine content and LUFS baking changes
the PCM samples -- but that's Phase 2A/2B wiring work, not this script's
job). Rows already under DUPES_MOVED_FOR_REVIEW are skipped -- that is a
review/quarantine holding area, not finalized content, and does not
belong in the pristine Archive tier.

Safety (same shape as musaeus_relink_recovered_dupes.py):
  - Dry run (no --execute) by default -- prints exactly what would move
    where and what the DB update would be, no writes.
  - --limit N caps how many rows are shown/touched, for reviewing a
    sample before running against everything.
  - Every row is re-verified live at run time (status still CATALOGUED,
    finalized_at still set, file still exists at its current path,
    target does not already exist) -- no reliance on a stale snapshot.
  - unique_path() prevents clobbering anything already at the target.
  - Collision-safe DB write (SOP §4.12): disk-side move happens first;
    if the DB UPDATE then fails, the file is moved back to its original
    location rather than left orphaned with a stale DB row.
  - Idempotent / resumable: rows whose file_path already starts with
    ALAC_ARCHIVE are skipped, so a second run only picks up what the
    first one missed.
  - Logs a MIGRATE_TO_ARCHIVE event per row (old_value/new_value) for a
    full audit trail, same shape as DupeResolver's/relink's own logging.

Deliberately NOT run yet: only build+dry-run this against the real vault
once Grey confirms Phase 1 is settled AND the live overnight pipeline
run (if any) has finished -- moving files while ALAC-Library is still
being actively written to by another process is exactly the concurrent-
writer risk this project's whole operating discipline exists to guard
against.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musaeus.config import MusicConfig  # noqa: E402
from musaeus.stages.organize import sanitize_path_component, unique_path  # noqa: E402

# Resolved from config, NOT hardcoded.
#
# These were literal paths under VAULT_ROOT ("ALAC-Library", "ALAC_Archive")
# when this was written on 2026-08-18. The libraries later moved under a
# Libraries/ subdirectory and this script was not updated, so every query it
# ran matched NOTHING and it reported "0 would migrate, 0 skipped" -- a
# confident, specific, wrong answer that would have said "nothing to do"
# for ever. Found 2026-09-05 only because the bake warned about 7,755 rows
# this script claimed did not exist.
#
# config is now the single source of these paths (alac_archive was not wired
# into it in August, which is why they were literals here; it is now).
_CFG = MusicConfig.from_env()
VAULT_ROOT = _CFG.vault_root
DB_PATH = _CFG.db_path
ALAC_LIBRARY = _CFG.alac_library
ALAC_ARCHIVE = _CFG.alac_archive
# Both locations: config puts the holding area under ALAC_Archive, but
# candidates are selected under ALAC_LIBRARY, so a same-named directory
# there would otherwise slip through. Verified empty on 2026-09-05; the
# second entry costs nothing and stops that being load-bearing.
DUPES_HOLDING_DIRS = (
    _CFG.dupes_review_dir,
    _CFG.alac_library / "DUPES_MOVED_FOR_REVIEW",
)


def _candidate_rows(conn: sqlite3.Connection) -> list[dict]:
    """
    Rows Phase 1 has fully finalized into ALAC-Library, not already
    migrated, not sitting in the DUPES_MOVED_FOR_REVIEW holding area.
    """
    rows = conn.execute(
        """
        SELECT id, file_path, artist, album
          FROM archive
         WHERE status = 'CATALOGUED'
           AND finalized_at IS NOT NULL
           AND file_path LIKE ? || '%'
         ORDER BY file_path
        """,
        (str(ALAC_LIBRARY),),
    ).fetchall()
    out = []
    for r in rows:
        fp = Path(r["file_path"])
        if any(str(fp).startswith(str(d)) for d in DUPES_HOLDING_DIRS):
            continue
        out.append(dict(r))
    return out


def _target_path(source: Path) -> Path:
    """
    Same relative shape as the source, just under ALAC_ARCHIVE instead
    of ALAC_LIBRARY -- preserves whatever dated-batch-folder/Artist/
    Album structure Finalize already gave it. Re-sanitizes each
    component defensively (cheap, and protects against a path that
    somehow predates a sanitize-rule fix) rather than assuming the
    existing path is already clean.
    """
    rel = source.relative_to(ALAC_LIBRARY)
    parts = [sanitize_path_component(p) for p in rel.parts[:-1]]
    return unique_path(ALAC_ARCHIVE.joinpath(*parts, rel.name))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate finalized ALAC-Library content into the new ALAC_Archive tier"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Only process the first N matching rows"
    )
    parser.add_argument(
        "--execute", action="store_true", help="Actually move files + update DB (default: dry run)"
    )
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = _candidate_rows(conn)
    if args.limit:
        rows = rows[: args.limit]

    mode = "EXECUTING" if args.execute else "DRY RUN (pass --execute to actually perform this)"
    print(f"{mode} — {len(rows)} row(s) to process\n")

    moved = 0
    skipped = 0
    for row in rows:
        source = Path(row["file_path"])

        # Live re-check -- never trust the snapshot taken above.
        current = conn.execute(
            "SELECT status, finalized_at FROM archive WHERE id = ?", (row["id"],)
        ).fetchone()
        if not current or current["status"] != "CATALOGUED" or not current["finalized_at"]:
            print(f"  SKIP  {source.name}: no longer a finalized CATALOGUED row")
            skipped += 1
            continue
        if not source.exists():
            print(f"  SKIP  {source.name}: file no longer exists at recorded path")
            skipped += 1
            continue

        target = _target_path(source)
        if target.exists():
            print(f"  SKIP  {source.name}: target already exists ({target})")
            skipped += 1
            continue

        print(f"  [{row.get('artist')} — {row.get('album')}] {source.name}")
        print(f"    from: {source}")
        print(f"    to:   {target}")
        print("    DB:   archive.file_path -> new path")

        if not args.execute:
            moved += 1
            continue

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
        except OSError as exc:
            print(f"    FAILED (move): {exc}", file=sys.stderr)
            skipped += 1
            continue

        try:
            conn.execute(
                "UPDATE archive SET file_path = ? WHERE id = ?",
                (str(target), row["id"]),
            )
            conn.execute(
                "INSERT INTO events (run_id, event_type, file_path, old_value, new_value, stage, note) "
                "VALUES (?, 'MIGRATE_TO_ARCHIVE', ?, ?, ?, 'migrate-to-archive', ?)",
                (
                    f"migrate_{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
                    str(target),
                    str(source),
                    str(target),
                    "ALAC-Library -> ALAC_Archive tier split",
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            # Collision-safe write (SOP §4.12): DB update failed -- move
            # the file back rather than leave it orphaned with a stale row.
            print(f"    FAILED (DB): {exc} -- reverting disk move", file=sys.stderr)
            try:
                shutil.move(str(target), str(source))
            except OSError as revert_exc:
                print(
                    f"    ALSO FAILED to revert move: {revert_exc} -- "
                    f"manual cleanup needed, file is at {target} but DB still says {source}",
                    file=sys.stderr,
                )
            skipped += 1
            continue

        moved += 1
        print("    done")

    print(f"\n{moved} {'migrated' if args.execute else 'would migrate'}, {skipped} skipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
