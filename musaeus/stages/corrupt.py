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

import logging
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ..context import StageResult
from ..deep_scan import ensure_columns as deep_scan_ensure_columns
from ..duration import duration_with_source
from .base import BaseStage

if TYPE_CHECKING:
    from ..context import RunContext

logger = logging.getLogger(__name__)

# A whole-file decode of a long hi-res track is minutes, not seconds.
_DECODE_TIMEOUT_S = 600

# ── Thresholds ────────────────────────────────────────────────────────────────

# Minimum expected bytes per second for each codec (conservative lower bound)
# Files below this ratio are likely truncated
MIN_BYTES_PER_SEC: dict[str, int] = {
    "flac": 30_000,  # ~240kbps minimum
    "alac": 30_000,
    "aac": 8_000,  # ~64kbps minimum
    "mp3": 8_000,
    # "m4a" here is a FILE EXTENSION key, only reached via check_file()'s
    # fallback when archive.codec is blank/unrecognized -- .m4a is a
    # container that can hold either ALAC (lossless) or AAC (lossy), so
    # this can NOT be treated as a confirmed AAC identification the way
    # "aac"/"mp3" above are (real ffprobe codec_name values). It's
    # intentionally set to the same conservative value as the "" unknown
    # default below, not a real AAC-specific threshold, precisely so an
    # ALAC-in-.m4a file with an unexpectedly blank codec doesn't get
    # held to a stricter lossless floor it might not deserve. By the
    # time CorruptStage runs (after ScholarStage in CANONICAL_PIPELINE),
    # codec should normally already be populated, so this fallback is a
    # rare, deliberately conservative safety net, not the common path.
    "m4a": 8_000,
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
    """The duration ffprobe REPORTS. Metadata, never a measurement.

    Delegates to musaeus.duration.duration_with_source, which does exactly
    this (stream first, container fallback) and is the single definition
    after 2026-09-02's audit found seven independent copies of "read the
    duration", split unnamed between container and stream. This function
    used to carry its own copy of the same two ffprobe calls; a semgrep
    rule written to guard against a THIRD copy caught this one still
    standing the same day.

    In MP4 -- every file in this library -- both stream and container
    duration live in the same moov atom, written before the audio, so both
    survive the audio being cut off. Measured 2026-09-02 on a truncated
    30 s file: stream said 30.0, container said 30.0, and 409 frames
    actually decoded. This docstring said "actual decoded duration" until
    that measurement -- exactly backwards, since nothing here decodes.
    Use ffmpeg_decode_check for that.
    """
    return duration_with_source(path)[0]


def check_file(path: Path, codec: str | None, duration_db: float | None) -> tuple[bool, str]:
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


def ffmpeg_decode_check(path: Path, seconds: int = 0) -> tuple[bool, str]:
    """Actually DECODE the audio and report whether ffmpeg found errors.

    CorruptStage's other checks are heuristics on the container -- filesize
    against duration, suspiciously short tracks. They describe the file's
    shape, never its contents, so a file whose header is fine and whose
    audio is damaged passes them cleanly.

    Two such files sat in the library undetected: `Aerosmith - What It
    Takes` and `Diana Ross - The Boss`, both raising "invalid zero block
    size" on decode. They surfaced 2026-08-31 only as a side effect of a
    full re-hash, which decodes as a byproduct -- not because anything was
    checking.

    Ported from ORPHEUS's SCRIPTS/orpheus_accurip_checker.py, with one
    change: ORPHEUS decodes the first 10 seconds, which cannot see damage
    later in the track. `seconds=0` decodes the whole file. The caller
    chooses, because a full decode over a large library is not free.
    """
    cmd = ["ffmpeg", "-v", "error", "-i", str(path)]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += ["-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_DECODE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, f"decode timed out after {_DECODE_TIMEOUT_S}s"
    except OSError as exc:
        return False, f"ffmpeg unavailable: {exc}"
    if proc.returncode != 0 or proc.stderr.strip():
        return False, (proc.stderr or f"ffmpeg exited {proc.returncode}").strip()[:200]
    return True, ""


class CorruptStage(BaseStage):
    """Detect and quarantine corrupt/truncated audio files."""

    NAME = "corrupt"

    #: Bound on how many never-checked, non-suspect files get decoded per
    #: run for the "right-sized but damaged" class check_file() cannot see
    #: at all. Overridable per-instance for tests. See the comment at its
    #: use site in _scan for why this exists and how the number was picked.
    NEW_ARRIVAL_DECODE_BUDGET = 200

    def validate(self, ctx: RunContext) -> None:
        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'"
        ).fetchone()[0]
        logger.info("[corrupt] %d CATALOGUED tracks to scan", count)

    def _scan(self, ctx: RunContext, dry_run: bool) -> StageResult:
        """Scan for corrupt files, optionally quarantine them."""
        result = self._make_result(dry_run=dry_run)
        conn = ctx.conn
        # The decode columns are owned by deep_scan and added lazily, so on
        # a database where deep_scan has never run they do not exist yet.
        # Reusing its helper rather than adding a sixth _ensure_columns.
        if not dry_run:
            deep_scan_ensure_columns(conn)

        # decode_ok/decode_checked_at exist once deep_scan_ensure_columns
        # has run at least once with dry_run=False. A dry run never calls
        # it (a preview must not alter schema), so on a database that has
        # never done a real run, selecting those columns would raise
        # "no such column". Select them only if they are actually there;
        # NULL stands in otherwise, which is exactly what "never checked"
        # means anyway.
        _has_decode_cols = {"decode_ok", "decode_checked_at"} <= {
            r[1] for r in conn.execute("PRAGMA table_info(archive)").fetchall()
        }
        decode_cols_sql = (
            "decode_ok, decode_checked_at" if _has_decode_cols
            else "NULL AS decode_ok, NULL AS decode_checked_at"
        )

        # Get all CATALOGUED tracks with file specs
        rows = conn.execute(
            f"""
            SELECT file_path, codec, duration, title, {decode_cols_sql}
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
        cleared = 0
        # Files never decode-checked by ANYTHING (this stage's suspect
        # path or deep_scan) that the size ratio does not flag either --
        # the class that hid Billy Joel, The Who, Billy Ocean, and others:
        # right-sized files with damage inside the stream, invisible to
        # check_file() by construction. Decoded here too, but BOUNDED.
        #
        # Unbounded would repeat the exact AcousticID mistake this project
        # already lived through: wired in unconditionally, its first run
        # was a full-library migration that held the DB write lock for
        # 21 hours. deep_scan exists precisely to pay that backlog down
        # -- idle-only, resumable, outside any pipeline run -- and this
        # budget exists so Act 1 never tries to do deep_scan's job
        # synchronously. 200 files at the ~1.46s/file measured 2026-09-02
        # is ~5 minutes added to a run with many new arrivals; a genuinely
        # large backlog is left for deep_scan rather than blocking here.
        new_arrival_decodes = 0
        quarantine_dir = ctx.vault_root / "QUARANTINE" / "corrupted"

        for row in rows:
            result.files_processed += 1
            file_path = Path(row["file_path"])

            if not file_path.exists():
                result.files_skipped += 1
                continue

            # Already known undecodable -- by THIS stage on a prior run, or
            # by deep_scan, which shares these columns rather than keeping
            # its own. Trust it rather than paying for a second decode of
            # a file already proven bad.
            if row["decode_ok"] == 0:
                is_corrupt = True
                reason = "previously found undecodable on decode (see decode_errors)"
                is_suspect = False
            else:
                is_suspect, reason = check_file(file_path, row["codec"], row["duration"])

                # The size ratio is a PRIORITISER, not a verdict -- deep_scan.py
                # says so and has the numbers: it flagged 418 files, of which 2
                # were damaged and 91 were undamaged Bing Crosby / Count Basie
                # mono recordings that genuinely compress to ~13% of PCM.
                #
                # This stage MOVES files to QUARANTINE. Acting on the ratio
                # alone meant physically removing ~91 good masters on a
                # heuristic that was right about 0.5% of the time.
                should_decode = is_suspect
                # The class the ratio cannot see at all: right-sized files
                # with the damage inside the stream. Decode a BOUNDED number
                # of never-checked, non-suspect files too, so genuinely new
                # arrivals get a real answer before dedup -- not just files
                # the shape heuristic happened to flag.
                if (
                    not is_suspect
                    and row["decode_checked_at"] is None
                    and new_arrival_decodes < self.NEW_ARRIVAL_DECODE_BUDGET
                ):
                    should_decode = True
                    new_arrival_decodes += 1

                if should_decode:
                    decoded_ok, decode_err = ffmpeg_decode_check(file_path, seconds=0)
                    if not dry_run:
                        conn.execute(
                            "UPDATE archive SET decode_checked_at = datetime('now'), "
                            "decode_ok = ?, decode_errors = ? WHERE file_path = ?",
                            (1 if decoded_ok else 0, 0 if decoded_ok else 1, str(file_path)),
                        )
                    if decoded_ok:
                        # Either flagged by shape and intact on inspection,
                        # or a never-checked file that turned out fine.
                        result.files_skipped += 1
                        if is_suspect:
                            logger.info(
                                "[%s] cleared: %s — %s, but decodes cleanly",
                                self.NAME,
                                file_path.name,
                                reason,
                            )
                            cleared += 1
                        continue
                    reason = f"{reason}; decode failed: {decode_err}" if is_suspect else (
                        f"decode failed: {decode_err}"
                    )

                # Reaching here without the "decoded cleanly" continue
                # above means: either should_decode was True and the decode
                # failed, or should_decode was False and nothing was
                # attempted this row. should_decode already equals
                # is_suspect in the second case, so this covers both.
                is_corrupt = should_decode

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
                            (file_path, issue, severity, run_id, checked_at)
                        VALUES (?, ?, ?, ?, datetime('now'))
                        -- Keyed without run_id, so a recurring issue UPDATES
                        -- its last-seen stamp instead of breeding a new row
                        -- every run. DO NOTHING here would freeze checked_at
                        -- at the first sighting and make the table look stale.
                        ON CONFLICT(file_path, issue) DO UPDATE SET
                            run_id     = excluded.run_id,
                            severity   = excluded.severity,
                            checked_at = excluded.checked_at
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

                        # Update archive status AND follow file_path to the
                        # new quarantine location -- the file physically
                        # moved, so a row still pointing at the old path
                        # would silently disagree with disk (the same class
                        # of bug found and fixed in dupe_resolver.py: a
                        # later stage reading file_path for this row would
                        # hit a "missing on disk" surprise instead of ever
                        # seeing the real quarantine location).
                        conn.execute(
                            "UPDATE archive SET status='QUARANTINED', file_path=? WHERE file_path=?",
                            (str(dest), str(file_path)),
                        )
                    except OSError as e:
                        logger.error(f"[{self.NAME}] Failed to quarantine: {e}")
                        result.files_errored += 1

        if not dry_run:
            conn.commit()

        # Summary
        if suspects:
            prefix = "Would quarantine" if dry_run else "Quarantined"
            result.notes.append(f"{prefix} {len(suspects)} corrupt file(s) to {quarantine_dir}")
            for s in suspects[:10]:  # Show first 10
                result.notes.append(f"  ⚠ {Path(s['file_path']).name}: {s['reason']}")
            if len(suspects) > 10:
                result.notes.append(f"  ... and {len(suspects) - 10} more")
        else:
            result.notes.append("✓ No corrupt files detected")

        # Say how many the cheap check accused and the decode acquitted.
        # Silence here would hide the whole point of the change: on this
        # library the ratio is expected to be wrong far more often than it
        # is right, and a stage that quietly drops those would look
        # identical to one that never flagged them.
        if cleared:
            result.notes.append(
                f"{cleared} file(s) flagged by size ratio but decoded cleanly — not corrupt"
            )

        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._scan(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._scan(ctx, dry_run=False)
