#!/usr/bin/env python3
"""
MUSAEUS — Stage: Acoustic
Detect near-duplicate tracks using Chromaprint acoustic fingerprints (fpcalc).

What it does:
  - Finds HASHED or CATALOGUED tracks that don't have a fingerprint yet
  - Runs fpcalc -json on each file to get acoustic fingerprint
  - Stores fingerprint in archive column: chromaprint TEXT
  - Compares fingerprints within same-artist groups (duration within 5s)
  - Uses difflib.SequenceMatcher for fingerprint similarity (>0.80 = dupe)
  - Stages duplicates in the duplicates table with type='ACOUSTIC'
  - Handles fpcalc errors gracefully (timeout=30s, log and skip)

Requirements:
  - fpcalc (Chromaprint) available in PATH
"""

from __future__ import annotations

import difflib
import json
import logging
import shutil
import subprocess
import uuid
from collections import defaultdict
from pathlib import Path

from ..context import RunContext, StageResult
from ..db import log_event
from .base import BaseStage, StageError

logger = logging.getLogger(__name__)

_FPCALC_TIMEOUT = 30  # seconds per file
_COMMIT_EVERY = 50
_DURATION_TOLERANCE = 5.0  # seconds
_SIMILARITY_THRESHOLD = 0.80


class AcousticStage(BaseStage):
    """
    Acoustic — fingerprint tracks with Chromaprint and detect acoustic duplicates.
    """

    NAME = "acoustic"

    # ── Validate ──────────────────────────────────────────────────────────────

    def validate(self, ctx: RunContext) -> None:
        if not shutil.which("fpcalc"):
            raise StageError("fpcalc not found — required for acoustic fingerprinting")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_pending(self, ctx: RunContext) -> list[dict]:
        """Return tracks that need fingerprinting."""
        rows = ctx.conn.execute(
            """
            SELECT file_path, artist, title, duration
            FROM archive
            WHERE status IN ('HASHED', 'CATALOGUED')
              AND (chromaprint IS NULL OR TRIM(chromaprint) = '')
            ORDER BY artist, album, track
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def _fingerprint_one(self, file_path: str) -> dict | None:
        """
        Run fpcalc -json on a file.
        Returns dict with 'fingerprint' and 'duration', or None on failure.
        """
        cmd = ["fpcalc", "-json", file_path]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=_FPCALC_TIMEOUT,
            )
            if proc.returncode != 0:
                logger.warning(
                    "fpcalc failed for %s: %s",
                    file_path,
                    proc.stderr.decode("utf-8", errors="replace")[:200],
                )
                return None

            data = json.loads(proc.stdout.decode("utf-8"))
            fingerprint = data.get("fingerprint")
            duration = data.get("duration")

            if not fingerprint:
                logger.warning("fpcalc returned no fingerprint for %s", file_path)
                return None

            return {"fingerprint": fingerprint, "duration": duration}

        except subprocess.TimeoutExpired:
            logger.warning("fpcalc timeout for %s (>%ds)", file_path, _FPCALC_TIMEOUT)
            return None
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("fpcalc error for %s: %s", file_path, exc)
            return None

    def _compare_fingerprints(self, fp1: str, fp2: str) -> float:
        """
        Compare two fingerprint strings using SequenceMatcher.
        Returns similarity ratio (0.0 to 1.0).
        """
        return difflib.SequenceMatcher(None, fp1, fp2).ratio()

    def _find_duplicates(self, ctx: RunContext) -> list[tuple[str, str, float]]:
        """
        Find acoustic duplicate pairs by comparing fingerprints
        within same-artist groups (where duration is within tolerance).
        Returns list of (file_path_1, file_path_2, similarity).
        """
        # Get all tracks with fingerprints
        rows = ctx.conn.execute(
            """
            SELECT file_path, artist, duration, chromaprint
            FROM archive
            WHERE chromaprint IS NOT NULL AND TRIM(chromaprint) != ''
              AND artist IS NOT NULL AND TRIM(artist) != ''
            ORDER BY artist, file_path
            """
        ).fetchall()

        # Group by normalized artist
        artist_groups: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            artist_key = r["artist"].strip().lower()
            artist_groups[artist_key].append(dict(r))

        dupes: list[tuple[str, str, float]] = []

        for artist_key, tracks in artist_groups.items():
            n = len(tracks)
            if n < 2:
                continue

            for i in range(n):
                for j in range(i + 1, n):
                    t1 = tracks[i]
                    t2 = tracks[j]

                    # Duration filter: skip if durations differ by more than tolerance
                    dur1 = t1.get("duration") or 0
                    dur2 = t2.get("duration") or 0
                    if abs(dur1 - dur2) > _DURATION_TOLERANCE:
                        continue

                    # Compare fingerprints
                    ratio = self._compare_fingerprints(
                        t1["chromaprint"], t2["chromaprint"]
                    )

                    if ratio > _SIMILARITY_THRESHOLD:
                        dupes.append((t1["file_path"], t2["file_path"], ratio))

        return dupes

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)

        pending = self._get_pending(ctx)
        total = len(pending)
        result.notes.append(f"tracks to fingerprint: {total}")

        if not total:
            result.notes.append("nothing to do — all tracks already fingerprinted")

        # Phase 1: Compute fingerprints
        fingerprinted = 0
        fp_errors = 0

        for i, row in enumerate(pending, 1):
            result.files_processed += 1
            file_path = row["file_path"]

            fp_result = self._fingerprint_one(file_path)

            if fp_result is None:
                fp_errors += 1
                result.files_errored += 1
                continue

            # Store fingerprint
            ctx.conn.execute(
                "UPDATE archive SET chromaprint = ? WHERE file_path = ?",
                (fp_result["fingerprint"], file_path),
            )

            log_event(
                ctx.conn,
                run_id=ctx.run_id,
                event_type="FINGERPRINT_COMPUTED",
                file_path=file_path,
                stage=self.NAME,
            )

            fingerprinted += 1
            result.files_changed += 1

            if i % _COMMIT_EVERY == 0:
                ctx.conn.commit()
                logger.info("[acoustic] fingerprint checkpoint %d/%d", i, total)

        ctx.conn.commit()

        result.notes.append(f"fingerprinted: {fingerprinted}")
        if fp_errors:
            result.notes.append(f"fingerprint errors: {fp_errors}")

        # Phase 2: Find acoustic duplicates
        dupes = self._find_duplicates(ctx)

        dupes_staged = 0
        for fp1, fp2, ratio in dupes:
            group_id = f"acoustic_{uuid.uuid4().hex[:8]}"

            ctx.conn.execute(
                """
                INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, status, run_id)
                VALUES (?, ?, 'ACOUSTIC', ?, 'pending', ?)
                """,
                (group_id, fp1, ratio, ctx.run_id),
            )
            ctx.conn.execute(
                """
                INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, status, run_id)
                VALUES (?, ?, 'ACOUSTIC', ?, 'pending', ?)
                """,
                (group_id, fp2, ratio, ctx.run_id),
            )

            log_event(
                ctx.conn,
                run_id=ctx.run_id,
                event_type="ACOUSTIC_DUPE_FOUND",
                file_path=fp1,
                new_value=fp2,
                stage=self.NAME,
                note=f"similarity={ratio:.3f}",
            )

            dupes_staged += 1
            logger.info(
                "acoustic dupe: %s ↔ %s (%.1f%%)",
                Path(fp1).name, Path(fp2).name, ratio * 100,
            )

        ctx.conn.commit()

        if dupes_staged:
            result.notes.append(f"acoustic duplicate pairs found: {dupes_staged}")
        else:
            result.notes.append("no acoustic duplicates detected")

        ctx.record_stage(result)
        return result

    # ── Dry run ───────────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=True)

        pending = self._get_pending(ctx)
        total = len(pending)

        result.files_processed = total
        result.notes.append(f"[DRY RUN] would fingerprint {total} file(s) using fpcalc")
        result.notes.append("  no fpcalc will be run, no DB changes")

        # Report existing fingerprints for dupe scanning context
        existing = ctx.conn.execute(
            """
            SELECT COUNT(*) FROM archive
            WHERE chromaprint IS NOT NULL AND TRIM(chromaprint) != ''
            """
        ).fetchone()[0]
        result.notes.append(
            f"  existing fingerprints: {existing} "
            f"(would be included in duplicate comparison)"
        )

        ctx.record_stage(result)
        return result
