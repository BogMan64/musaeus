#!/usr/bin/env python3
"""
MUSAEUS — Stage: Reviewer
Groq AI metadata quality review for CATALOGUED tracks.

What it does:
  - Samples CATALOGUED tracks in batches (default 50 per run)
  - Sends each batch to Groq (llama-3.3-70b-versatile) for review
  - AI checks: suspicious titles, likely wrong genre, ALL-CAPS fields,
    article errors, probable duplicates, missing metadata
  - Stores AI findings in a review_issues table (auto-created)
  - Logs REVIEWER_ISSUE event for each finding
  - dry_run() shows what would be reviewed without writing to DB
  - Idempotent: already-reviewed files are skipped

AI prompt strategy:
  - Batches of 20 tracks sent as JSON
  - AI returns structured JSON: [{file_path, issue_type, detail, confidence}]
  - Responses are validated before storage
  - Low-confidence findings (< 0.6) are discarded

Requirements:
  - GROQ_API_KEY in ~/.config/musaeus/settings.env

Graceful degradation:
  - No API key → stage skipped with warning
  - API error → batch skipped, run continues
  - Malformed response → batch skipped, logged

ORPHEUS equivalent: SCRIPTS/orpheus_reviewer_hybrid_v4.py
"""

from __future__ import annotations

import json
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..context import RunContext, StageResult
from .base import BaseStage

logger = logging.getLogger(__name__)

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_MODEL = "llama-3.3-70b-versatile"
_BATCH_SIZE = 20  # tracks per API call
_DEFAULT_MAX_FILES = 50  # files reviewed per run (overnight-safe)
_RATE_LIMIT_S = 2.0  # groq free tier is generous but be polite
_TIMEOUT_S = 30
_MIN_CONFIDENCE = 0.6
_COMMIT_EVERY = 50

_SYSTEM_PROMPT = """\
You are a music metadata quality reviewer. You will receive a JSON array of tracks.
For each track, identify metadata quality issues.

Return ONLY a valid JSON array of issue objects. Each object must have:
  - "file_path": (string) exact file_path from input
  - "issue_type": one of: WRONG_GENRE, SUSPICIOUS_TITLE, MISSING_METADATA,
                          ALL_CAPS_FIELD, ARTICLE_ERROR, PROBABLE_DUPLICATE,
                          ENCODING_ARTIFACT, YEAR_SUSPICIOUS
  - "detail": (string) brief human-readable explanation (max 100 chars)
  - "confidence": (float 0.0-1.0) how confident you are this is a real issue

Only report genuine issues. Do not invent problems.
If a track looks fine, do not include it.
Return [] if no issues found.
"""

_USER_TEMPLATE = """\
Review these tracks for metadata quality issues:

{tracks_json}

Return a JSON array of issues only. No prose, no markdown, just the JSON array.
"""


# ── DB helpers ────────────────────────────────────────────────────────────────


def _ensure_table(conn) -> None:  # type: ignore[type-arg]
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_issues (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path   TEXT NOT NULL,
            issue_type  TEXT NOT NULL,
            detail      TEXT,
            confidence  REAL,
            run_id      TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        )
        """
    )
    # Index for fast per-file lookups
    conn.execute("CREATE INDEX IF NOT EXISTS idx_review_issues_path ON review_issues(file_path)")
    conn.commit()


def _already_reviewed(conn, file_path: str) -> bool:  # type: ignore[type-arg]
    try:
        return bool(
            conn.execute(
                "SELECT 1 FROM review_issues WHERE file_path=? LIMIT 1",
                (file_path,),
            ).fetchone()
        )
    except Exception:
        return False


# ── Groq API ──────────────────────────────────────────────────────────────────


def _groq_review(
    tracks: list[dict],
    api_key: str,
) -> list[dict]:
    """
    Send a batch of tracks to Groq for review.
    Returns list of issue dicts (validated).
    """
    payload = json.dumps(
        {
            "model": _GROQ_MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _USER_TEMPLATE.format(tracks_json=json.dumps(tracks, indent=2)),
                },
            ],
            "temperature": 0.1,
            "max_tokens": 2048,
        }
    ).encode("utf-8")

    req = Request(
        _GROQ_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(req, timeout=_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raise ValueError(f"Groq API HTTP {exc.code}: {exc.reason}") from exc
    except URLError as exc:
        raise ValueError(f"Groq API network error: {exc.reason}") from exc

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "[]").strip()

    # Strip markdown code fences if present
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]

    try:
        issues = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Could not parse Groq response as JSON: {exc}") from exc

    if not isinstance(issues, list):
        raise ValueError(f"Expected JSON array, got {type(issues).__name__}")

    # Validate and filter
    valid = []
    for item in issues:
        if not isinstance(item, dict):
            continue
        fp = item.get("file_path", "")
        issue_type = item.get("issue_type", "")
        confidence = float(item.get("confidence", 0))
        if fp and issue_type and confidence >= _MIN_CONFIDENCE:
            valid.append(
                {
                    "file_path": fp,
                    "issue_type": issue_type[:40],
                    "detail": str(item.get("detail", ""))[:200],
                    "confidence": confidence,
                }
            )

    return valid


# ── Stage ─────────────────────────────────────────────────────────────────────


class ReviewerStage(BaseStage):
    """
    Reviewer — Groq AI metadata quality review for CATALOGUED tracks.
    """

    NAME = "reviewer"

    def validate(self, ctx: RunContext) -> None:
        api_key = ctx.config.groq_api_key
        if not api_key:
            logger.warning(
                "[reviewer] GROQ_API_KEY not set — stage will be a no-op. "
                "Add it to ~/.config/musaeus/settings.env"
            )

        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'"
        ).fetchone()[0]
        logger.info("[reviewer] %d CATALOGUED track(s) in library", count)

    def _review(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)

        api_key = ctx.config.groq_api_key
        if not api_key:
            result.notes.append(
                "GROQ_API_KEY not set — skipping AI review. "
                "Set it in ~/.config/musaeus/settings.env"
            )
            ctx.record_stage(result)
            return result

        if not dry_run:
            _ensure_table(ctx.conn)

        max_files = ctx.get("reviewer_max_files", _DEFAULT_MAX_FILES)

        rows = ctx.conn.execute(
            """
            SELECT file_path, artist, album, title, genre, year,
                   bitrate, codec, duration
            FROM archive
            WHERE status = 'CATALOGUED'
            ORDER BY file_path
            """
        ).fetchall()

        # Filter already-reviewed unless dry_run
        if not dry_run:
            rows = [r for r in rows if not _already_reviewed(ctx.conn, r["file_path"])]

        if max_files and len(rows) > max_files:
            logger.info("[reviewer] capping %d rows to %d", len(rows), max_files)
            rows = rows[:max_files]

        total_issues = 0
        batches_ok = 0
        batches_err = 0

        for batch_start in range(0, len(rows), _BATCH_SIZE):
            batch = rows[batch_start : batch_start + _BATCH_SIZE]
            result.files_processed += len(batch)

            tracks_for_ai = [
                {
                    "file_path": r["file_path"],
                    "artist": r["artist"] or "",
                    "album": r["album"] or "",
                    "title": r["title"] or "",
                    "genre": r["genre"] or "",
                    "year": r["year"] or "",
                    "bitrate": r["bitrate"] or "",
                    "codec": r["codec"] or "",
                }
                for r in batch
            ]

            time.sleep(_RATE_LIMIT_S)

            try:
                issues = _groq_review(tracks_for_ai, api_key)
                batches_ok += 1
            except Exception as exc:
                logger.warning("[reviewer] batch error: %s", exc)
                batches_err += 1
                result.errors.append(str(exc))
                continue

            for issue in issues:
                total_issues += 1
                result.files_changed += 1
                logger.info(
                    "[reviewer] %s  %s  %.0f%%  %s",
                    issue["issue_type"],
                    issue["confidence"],
                    issue["confidence"] * 100,
                    issue["detail"],
                )
                if not dry_run:
                    ctx.conn.execute(
                        """
                        INSERT INTO review_issues
                            (file_path, issue_type, detail, confidence, run_id)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            issue["file_path"],
                            issue["issue_type"],
                            issue["detail"],
                            issue["confidence"],
                            ctx.run_id,
                        ),
                    )
                    ctx.log_event(
                        "REVIEWER_ISSUE",
                        file_path=issue["file_path"],
                        new_value=issue["issue_type"],
                        stage=self.NAME,
                        note=f"confidence={issue['confidence']:.2f} {issue['detail'][:60]}",
                    )

            if (batch_start // _BATCH_SIZE) % (
                _COMMIT_EVERY // _BATCH_SIZE + 1
            ) == 0 and not dry_run:
                ctx.conn.commit()

        prefix = "Would review" if dry_run else "Reviewed"
        result.notes.append(
            f"{prefix} {result.files_processed} track(s) in "
            f"{batches_ok} batch(es): {total_issues} issue(s) found."
        )
        if batches_err:
            result.notes.append(f"{batches_err} batch error(s) — check logs.")
        if total_issues:
            result.notes.append("Run `musaeus review-report` to see all issues.")

        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._review(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._review(ctx, dry_run=False)
