#!/usr/bin/env python3
"""
MUSAEUS — Stage: Health
Library-wide consistency and quality checks.

What it does:
  - Scans every CATALOGUED archive row for known issues
  - Writes issues to the validation_issues table
  - Does NOT modify any audio files or archive rows
  - dry_run() is identical to run() (read-only by design)

Checks performed:
  MISSING_TITLE      — title tag is NULL or blank
  MISSING_ARTIST     — artist tag is NULL or blank
  MISSING_ALBUM      — album tag is NULL or blank
  MISSING_YEAR       — year tag is NULL or blank
  MISSING_GENRE      — genre tag is NULL or blank
  ZERO_DURATION      — duration is NULL, 0, or < 1 second
  SUSPICIOUS_BITRATE — bitrate is NULL, < 64 kbps, or > 10,000 kbps
  MISSING_TRACK      — track number is NULL (warning only)
  LOW_QUALITY        — bitrate < 128 kbps (warning)
  LOSSLESS_EXPECTED  — .flac/.alac file has bitrate < 300 kbps (warning)
  NO_AUDIO_HASH      — audio_hash is NULL (Sentinel hasn't run yet)
  UNKNOWN_CODEC      — codec is NULL

Severity levels:
  error   — data is definitely wrong (zero duration, missing artist+title)
  warning — data is suspicious or incomplete
"""

from __future__ import annotations

import logging

from ..context import RunContext, StageResult
from .base import BaseStage

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 200


def _check_row(row: dict) -> list[tuple[str, str]]:
    """
    Return a list of (issue_code, severity) for a single archive row.
    """
    issues: list[tuple[str, str]] = []

    def _blank(v: object) -> bool:
        return v is None or str(v).strip() == ""

    # Required tags
    if _blank(row.get("title")):
        issues.append(("MISSING_TITLE", "error"))
    if _blank(row.get("artist")):
        issues.append(("MISSING_ARTIST", "error"))
    if _blank(row.get("album")):
        issues.append(("MISSING_ALBUM", "warning"))
    if _blank(row.get("year")):
        issues.append(("MISSING_YEAR", "warning"))
    if _blank(row.get("genre")):
        issues.append(("MISSING_GENRE", "warning"))
    if row.get("track") is None:
        issues.append(("MISSING_TRACK", "warning"))

    # Duration checks
    dur = row.get("duration")
    if dur is None or float(dur) < 1.0:
        issues.append(("ZERO_DURATION", "error"))

    # Bitrate checks
    br = row.get("bitrate")
    if br is None:
        issues.append(("SUSPICIOUS_BITRATE", "error"))
    else:
        br_int = int(br)
        if br_int < 64_000 or br_int > 10_000_000:
            issues.append(("SUSPICIOUS_BITRATE", "error"))
        elif br_int < 128_000:
            issues.append(("LOW_QUALITY", "warning"))

    # Lossless format but low bitrate. Deliberately codec-first, not
    # extension-first: .m4a can hold either ALAC (lossless) or AAC
    # (lossy) -- codec in ("alac", "flac") is what actually catches an
    # ALAC-in-.m4a file. The ext == ".flac" branch only exists because
    # .flac unambiguously means lossless by extension alone, unlike
    # .m4a. See config.py's LOSSLESS_CODECS comment and
    # canonicalize.py's module docstring for the concrete bug this
    # class of extension-only check caused elsewhere in the codebase.
    ext = (row.get("ext") or "").lower()
    codec = (row.get("codec") or "").lower()
    if (ext == ".flac" or codec in ("alac", "flac")) and br is not None and int(br) < 300_000:
        issues.append(("LOSSLESS_EXPECTED", "warning"))

    # Hash
    if not row.get("audio_hash"):
        issues.append(("NO_AUDIO_HASH", "warning"))

    # Codec
    if _blank(row.get("codec")):
        issues.append(("UNKNOWN_CODEC", "warning"))

    return issues


class HealthStage(BaseStage):
    """
    Health check — scan archive for quality and consistency issues.
    Results are written to validation_issues table.
    """

    NAME = "health"

    # ── Validate ──────────────────────────────────────────────────────────────

    def validate(self, ctx: RunContext) -> None:
        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status NOT IN ('GHOST','PENDING')"
        ).fetchone()[0]
        if count == 0:
            logger.info("[health] no catalogued files to check")

    # ── Shared scan logic ─────────────────────────────────────────────────────

    def _scan(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)

        rows = ctx.conn.execute(
            """
            SELECT file_path, title, artist, album, genre, year, track,
                   duration, bitrate, codec, ext, audio_hash, status
            FROM archive
            WHERE status NOT IN ('GHOST')
            ORDER BY artist, album, title
            """
        ).fetchall()

        issue_counts: dict[str, int] = {}
        total_issues = 0

        for row in rows:
            result.files_processed += 1
            issues = _check_row(dict(row))

            if not issues:
                result.files_skipped += 1
                continue

            result.files_changed += 1
            total_issues += len(issues)

            for issue_code, severity in issues:
                issue_counts[issue_code] = issue_counts.get(issue_code, 0) + 1

                if not dry_run:
                    ctx.conn.execute(
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
                        (row["file_path"], issue_code, severity, ctx.run_id),
                    )

            if result.files_processed % _COMMIT_EVERY == 0 and not dry_run:
                ctx.conn.commit()
                logger.info("[health] checkpoint %d", result.files_processed)

        # Summary notes
        if total_issues == 0:
            result.notes.append("✓ No issues found — library looks healthy.")
        else:
            prefix = "Would log" if dry_run else "Logged"
            result.notes.append(
                f"{prefix} {total_issues} issue(s) across {result.files_changed} file(s):"
            )
            for code, count in sorted(issue_counts.items(), key=lambda x: -x[1]):
                result.notes.append(f"  {code}: {count}")

        ctx.record_stage(result)
        return result

    # ── Dry run ───────────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._scan(ctx, dry_run=True)

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self, ctx: RunContext) -> StageResult:
        return self._scan(ctx, dry_run=False)
