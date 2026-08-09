#!/usr/bin/env python3
"""
MUSAEUS — Corruption Detection Stage

Detects truncated or corrupt audio files via ffprobe analysis.

What it does:
  1. Checks filesize vs duration ratio (codec-appropriate thresholds)
  2. Flags suspiciously short tracks (<45s) unless they match keywords
  3. Quarantines corrupt files to VAULT/QUARANTINE/corrupted/ (if --apply)
  4. Reports findings to validation_issues table

Based on ORPHEUS orpheus_corrupt_detector.py
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ..context import StageResult
from .base import BaseStage

if TYPE_CHECKING:
    from ..context import RunContext

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────

# Minimum expected bytes per second for each codec (conservative lower bound)
# Files below this ratio are likely truncated
MIN_BYTES_PER_SEC: dict[str, int] = {
    "flac": 30_000,  # ~240kbps minimum
    "alac": 30_000,
    "aac": 8_000,  # ~64kbps minimum
    "mp3": 8_000,
    "m4a": 8_000,  # AAC
    "wav": 88_000,  # 44.1kHz 16-bit stereo
    "pcm_s16le": 88_000,
    "": 8_000,  # unknown — be conservative
}

# Tracks shorter than this are flagged as suspiciously short
MIN_DURATION_SEC = 45  # 45 seconds

# Short-track keywords — these are allowed to be short
SHORT_OK_KEYWORDS = re.compile(
    r"\b(intro|outro|skit|reprise|interlude|snippet|clip|"
    r"bonus|fragment|excerpt|medley|bridge)\b",
    re.IGNORECASE,
)


def ffprobe_duration(path: Path) -> float | None:
    """Get actual decoded duration in seconds via ffprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        data = json.loads(r.stdout)
        streams = data.get("streams", [])
        if streams and streams[0].get("duration"):
            return float(streams[0]["duration"])

        # Fallback: format duration
        cmd2 = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ]
        r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=15)
        data2 = json.loads(r2.stdout)
        dur = data2.get("format", {}).get("duration")
        return float(dur) if dur else None
    except Exception:
        return None


def check_file(
    path: Path, codec: str | None, duration_db: float | None
) -> tuple[bool, str]:
    """
    Check if file is corrupt based on size/duration ratio.
    Returns (is_suspect, reason).
    """
    try:
        size_bytes = path.stat().st_size
    except OSError:
        return True, "Cannot read file"

    # Get duration from DB or ffprobe fallback
    declared_sec = duration_db
    if not declared_sec or declared_sec <= 0:
        declared_sec = ffprobe_duration(path)

    if not declared_sec or declared_sec <= 0:
        return False, ""  # Can't check without duration

    # Determine codec for threshold
    codec_key = (codec or "").lower()
    if codec_key not in MIN_BYTES_PER_SEC:
        # Try file extension
        codec_key = path.suffix.lstrip(".").lower()

    min_bps = MIN_BYTES_PER_SEC.get(codec_key, MIN_BYTES_PER_SEC[""])
    expected_min_bytes = min_bps * declared_sec

    # Check 1: filesize vs duration ratio (allow 15% tolerance)
    if size_bytes < expected_min_bytes * 0.15:
        size_kb = size_bytes / 1024
        expected_kb = expected_min_bytes / 1024
        reason = (
            f"filesize {size_kb:.0f}KB too small for "
            f"{declared_sec:.0f}s {codec_key} "
            f"(expected ≥{expected_kb:.0f}KB)"
        )
        return True, reason

    # Check 2: suspiciously short duration
    if declared_sec < MIN_DURATION_SEC:
        title = path.stem
        if not SHORT_OK_KEYWORDS.search(title):
            reason = f"duration {declared_sec:.0f}s suspiciously short"
            return True, reason

    return False, ""


class CorruptStage(BaseStage):
    """Detect and quarantine corrupt/truncated audio files."""

    NAME = "corrupt"

    def validate(self, ctx: RunContext) -> None:
        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'"
        ).fetchone()[0]
        logger.info("[corrupt] %d CATALOGUED tracks to scan", count)

    def _scan(self, ctx: RunContext, dry_run: bool) -> StageResult:
        """Scan for corrupt files, optionally quarantine them."""
        result = self._make_result(dry_run=dry_run)
        conn = ctx.conn

        # Get all CATALOGUED tracks with file specs
        rows = conn.execute(
            """
            SELECT file_path, codec, duration, title
            FROM archive
            WHERE status = 'CATALOGUED'
              AND file_path IS NOT NULL
            ORDER BY artist, album
            """
        ).fetchall()

        if not rows:
            logger.info("[corrupt] No CATALOGUED tracks to scan")
            result.notes.append("No tracks to scan")
            ctx.record_stage(result)
            return result

        logger.info(f"[{self.NAME}] Scanning {len(rows):,} tracks for corruption...")

        suspects: list[dict] = []
        quarantine_dir = ctx.vault_root / "QUARANTINE" / "corrupted"

        for row in rows:
            result.files_processed += 1
            file_path = Path(row["file_path"])

            if not file_path.exists():
                result.files_skipped += 1
                continue

            is_corrupt, reason = check_file(
                file_path, row["codec"], row["duration"]
            )

            if is_corrupt:
                result.files_changed += 1
                logger.warning(f"[{self.NAME}] ⚠ {file_path.name}")
                logger.warning(f"[{self.NAME}]   {reason}")

                suspects.append(
                    {
                        "file_path": str(file_path),
                        "reason": reason,
                        "title": row["title"],
                    }
                )

                # Log to validation_issues
                if not dry_run:
                    conn.execute(
                        """
                        INSERT INTO validation_issues
                            (file_path, issue, severity, run_id)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(file_path, issue, run_id) DO NOTHING
                        """,
                        (str(file_path), "CORRUPT_FILE", "error", ctx.run_id),
                    )

                    ctx.log_event(
                        "CORRUPT_DETECTED",
                        file_path=str(file_path),
                        stage=self.NAME,
                        note=reason,
                    )

                    # Quarantine file
                    quarantine_dir.mkdir(parents=True, exist_ok=True)
                    dest = quarantine_dir / file_path.name

                    # Handle name collisions
                    if dest.exists():
                        counter = 1
                        while dest.exists():
                            dest = quarantine_dir / f"{file_path.stem}_{counter}{file_path.suffix}"
                            counter += 1

                    try:
                        import shutil
                        shutil.move(str(file_path), str(dest))
                        logger.info(f"[{self.NAME}] → Quarantined to {dest.name}")

                        # Update archive status
                        conn.execute(
                            "UPDATE archive SET status='QUARANTINED' WHERE file_path=?",
                            (str(file_path),),
                        )
                    except OSError as e:
                        logger.error(f"[{self.NAME}] Failed to quarantine: {e}")
                        result.files_errored += 1

        if not dry_run:
            conn.commit()

        # Summary
        if suspects:
            prefix = "Would quarantine" if dry_run else "Quarantined"
            result.notes.append(
                f"{prefix} {len(suspects)} corrupt file(s) to {quarantine_dir}"
            )
            for s in suspects[:10]:  # Show first 10
                result.notes.append(f"  ⚠ {Path(s['file_path']).name}: {s['reason']}")
            if len(suspects) > 10:
                result.notes.append(f"  ... and {len(suspects) - 10} more")
        else:
            result.notes.append("✓ No corrupt files detected")

        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._scan(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._scan(ctx, dry_run=False)
