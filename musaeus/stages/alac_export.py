#!/usr/bin/env python3
"""
MUSAEUS — Stage: ALAC Export
Build an ALAC archive library from FLAC source files using ffmpeg.

What it does:
  - Finds CATALOGUED or forged tracks with ext='.flac'
  - Converts FLAC → ALAC (.m4a) using ffmpeg
  - Preserves artist/album folder structure: ALAC_Library/Artist/Album/track.m4a
  - Skips files already exported (output exists and is newer)
  - Stores export path in archive column: alac_export_path TEXT
  - Handles conversion errors gracefully (log, increment errors, continue)

Requirements:
  - ffmpeg available in PATH
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from ..config import get_config
from ..context import RunContext, StageResult
from ..db import log_event
from .base import BaseStage, StageError

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 25
_FFMPEG_TIMEOUT = 300  # 5 minutes per file


class AlacExportStage(BaseStage):
    """
    ALAC Export — convert FLAC files to ALAC (.m4a) for Apple ecosystem.
    """

    NAME = "alac_export"

    # ── Validate ──────────────────────────────────────────────────────────────

    def validate(self, ctx: RunContext) -> None:
        if not shutil.which("ffmpeg"):
            raise StageError("ffmpeg not found — required for ALAC conversion")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_export_root(self, ctx: RunContext) -> Path:
        """Determine the ALAC export root directory."""
        stash_root = ctx.get("alac_export_root")
        if stash_root:
            return Path(stash_root)
        cfg = get_config()
        return cfg.vault_root / "ALAC_Library"

    def _get_pending(self, ctx: RunContext) -> list[dict]:
        """Return FLAC tracks eligible for ALAC export."""
        rows = ctx.conn.execute(
            """
            SELECT file_path, artist, album, title, filename
            FROM archive
            WHERE status IN ('CATALOGUED', 'FORGED')
              AND LOWER(ext) = '.flac'
            ORDER BY artist, album, track
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def _compute_output_path(self, row: dict, export_root: Path) -> Path:
        """Compute the ALAC output path preserving artist/album structure."""
        artist = (row.get("artist") or "Unknown Artist").strip()
        album = (row.get("album") or "Unknown Album").strip()

        # Sanitize for filesystem
        artist_safe = "".join(c for c in artist if c not in '<>:"/\\|?*').strip() or "Unknown Artist"
        album_safe = "".join(c for c in album if c not in '<>:"/\\|?*').strip() or "Unknown Album"

        # Derive filename from source, change extension to .m4a
        source_name = Path(row.get("filename") or Path(row["file_path"]).name).stem
        output_name = f"{source_name}.m4a"

        return export_root / artist_safe / album_safe / output_name

    def _should_skip(self, source: Path, output: Path) -> bool:
        """Check if the output already exists and is up-to-date."""
        if not output.exists():
            return False
        # Skip if output is newer than source
        return output.stat().st_mtime >= source.stat().st_mtime

    def _convert_one(self, source: Path, output: Path) -> bool:
        """Convert a single FLAC file to ALAC. Returns True on success."""
        output.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "ffmpeg", "-y",
            "-i", str(source),
            "-acodec", "alac",
            "-vn",
            str(output),
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=_FFMPEG_TIMEOUT,
            )
            if proc.returncode != 0:
                logger.warning(
                    "ffmpeg failed for %s: %s",
                    source, proc.stderr.decode("utf-8", errors="replace")[:200],
                )
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg timeout for %s (>%ds)", source, _FFMPEG_TIMEOUT)
            return False
        except OSError as exc:
            logger.warning("ffmpeg OSError for %s: %s", source, exc)
            return False

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)
        export_root = self._get_export_root(ctx)
        pending = self._get_pending(ctx)
        total = len(pending)
        result.notes.append(f"FLAC files eligible: {total}")
        result.notes.append(f"export root: {export_root}")

        if not total:
            result.notes.append("nothing to do — no FLAC files found")
            ctx.record_stage(result)
            return result

        converted = 0
        skipped = 0
        errors = 0

        for i, row in enumerate(pending, 1):
            result.files_processed += 1
            source = Path(row["file_path"])
            output = self._compute_output_path(row, export_root)

            # Skip if already exported and up-to-date
            if self._should_skip(source, output):
                skipped += 1
                result.files_skipped += 1
                continue

            # Check source exists
            if not source.exists():
                logger.warning("source file missing: %s", source)
                result.files_errored += 1
                errors += 1
                continue

            # Convert
            if self._convert_one(source, output):
                # Update archive
                ctx.conn.execute(
                    "UPDATE archive SET alac_export_path = ? WHERE file_path = ?",
                    (str(output), row["file_path"]),
                )

                log_event(
                    ctx.conn,
                    run_id=ctx.run_id,
                    event_type="ALAC_EXPORTED",
                    file_path=row["file_path"],
                    new_value=str(output),
                    stage=self.NAME,
                )

                converted += 1
                result.files_changed += 1
                logger.info("ALAC exported: %s → %s", source.name, output)
            else:
                errors += 1
                result.files_errored += 1

            if i % _COMMIT_EVERY == 0:
                ctx.conn.commit()
                logger.info("[alac_export] checkpoint %d/%d", i, total)

        ctx.conn.commit()

        result.notes.append(f"converted: {converted}")
        if skipped:
            result.notes.append(f"skipped (already exported): {skipped}")
        if errors:
            result.notes.append(f"errors: {errors}")

        if errors > 0 and converted == 0:
            result.success = False

        ctx.record_stage(result)
        return result

    # ── Dry run ───────────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=True)
        export_root = self._get_export_root(ctx)
        pending = self._get_pending(ctx)
        total = len(pending)

        would_convert = 0
        would_skip = 0
        estimated_size = 0

        for row in pending:
            source = Path(row["file_path"])
            output = self._compute_output_path(row, export_root)

            if self._should_skip(source, output):
                would_skip += 1
            else:
                would_convert += 1
                # Estimate ALAC size as ~60% of FLAC (rough approximation)
                if source.exists():
                    estimated_size += int(source.stat().st_size * 0.6)

        result.files_processed = total
        result.notes.append(f"[DRY RUN] export root: {export_root}")
        result.notes.append(f"[DRY RUN] would convert: {would_convert} file(s)")
        result.notes.append(f"[DRY RUN] would skip (already exported): {would_skip}")
        if estimated_size:
            size_mb = estimated_size / (1024 * 1024)
            result.notes.append(f"[DRY RUN] estimated output size: ~{size_mb:.0f} MB")

        ctx.record_stage(result)
        return result
