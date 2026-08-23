#!/usr/bin/env python3
"""
MUSAEUS — Stage: AcousticID
Acoustic fingerprint-based duplicate detection using fpcalc + AcousticID API.

What it does:
  - Finds CATALOGUED rows that haven't been fingerprinted yet
  - Runs fpcalc to generate a Chromaprint fingerprint for each file
  - Queries AcousticID API to get the recording MBID for each fingerprint
  - Stores fingerprint + recording_id in archive
    (columns auto-added on first run via _ensure_columns)
  - Detects ACOUSTIC duplicates: same recording_id → different file_path
  - Stages duplicate pairs in the duplicates table with type='ACOUSTIC'
  - Logs ACOUSTIC_MATCHED / ACOUSTIC_DUPE_FOUND events
  - dry_run() reports matches without any DB changes

Why this matters beyond Sentinel:
  - Sentinel hashes the raw PCM stream — catches exact bitwise copies
  - AcousticID catches re-encodes: same song, FLAC vs MP3 vs different bitrate
  - These are the most common "same song twice" scenarios in real libraries

Requirements:
  - fpcalc binary (from chromaprint package) must be in PATH
  - ACOUSTICID_API_KEY in ~/.config/musaeus/settings.env
    (free key at acousticid.org — 3 req/s limit)

Graceful degradation:
  - fpcalc not found → stage skipped with warning
  - API key missing → fingerprints stored but recording IDs not looked up
  - API error → file skipped, run continues
  - Rate-limited → backs off 1s and retries once

ORPHEUS equivalent: SCRIPTS/stage_acoustic_fingerprint_duplicates.py
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.error
import uuid
from urllib.parse import urlencode
from urllib.request import urlopen

from ..context import RunContext, StageResult
from ..network_policy import check as _network_check
from .base import BaseStage, StageError

logger = logging.getLogger(__name__)

_ACOUSTICID_URL = "https://api.acoustid.org/v2/lookup"
_RATE_LIMIT_S = 0.34  # 3 req/s limit for free tier
_TIMEOUT_S = 15
_FPCALC_TIMEOUT_S = 60  # fpcalc on a large FLAC can take ~10s
_COMMIT_EVERY = 50
_MIN_DURATION_S = 30  # skip clips shorter than 30s (too unreliable)


# ── Column migration ──────────────────────────────────────────────────────────


def _ensure_columns(conn) -> None:  # type: ignore[type-arg]
    existing = {row[1] for row in conn.execute("PRAGMA table_info(archive)").fetchall()}
    for col, typedef in (
        ("chromaprint", "TEXT"),
        ("chromaprint_duration", "REAL"),
        ("acousticid_recording", "TEXT"),
        ("acousticid_score", "REAL"),
        ("acousticid_checked_at", "TEXT"),
    ):
        if col not in existing:
            conn.execute(f"ALTER TABLE archive ADD COLUMN {col} {typedef}")
    conn.commit()


# ── fpcalc wrapper ────────────────────────────────────────────────────────────


def _fpcalc(path: str) -> tuple[float, str]:
    """
    Run fpcalc on an audio file.
    Returns (duration_seconds, fingerprint_string).
    Raises RuntimeError or ValueError on failure.
    """
    import shutil

    fpcalc = shutil.which("fpcalc")
    if not fpcalc:
        raise RuntimeError("fpcalc not found in PATH")

    try:
        res = subprocess.run(
            [fpcalc, "-json", path],
            capture_output=True,
            text=True,
            timeout=_FPCALC_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"fpcalc timed out after {_FPCALC_TIMEOUT_S}s") from exc

    if res.returncode != 0:
        raise ValueError(f"fpcalc failed (rc={res.returncode}): {res.stderr[:200]}")

    data = json.loads(res.stdout)
    duration = float(data.get("duration", 0))
    fingerprint = data.get("fingerprint", "")
    if not fingerprint:
        raise ValueError("fpcalc returned empty fingerprint")
    return duration, fingerprint


# ── AcousticID API ────────────────────────────────────────────────────────────


def _acousticid_lookup(fingerprint: str, duration: float, api_key: str) -> tuple[str, float] | None:
    """
    Query AcousticID for a recording match.
    Returns (recording_id, score) or None if no confident match.
    """
    params = {
        "client": api_key,
        "fingerprint": fingerprint,
        "duration": str(int(duration)),
        "meta": "recordings",
        "format": "json",
    }
    url = f"{_ACOUSTICID_URL}?{urlencode(params)}"

    for attempt in range(2):
        try:
            # Ask the gateway before dispatching. Under LOCAL_ONLY this raises,
            # and the attempt is recorded BEFORE raising so the broad except
            # below cannot erase the evidence -- see network_policy.py.
            _network_check(_ACOUSTICID_URL)
            with urlopen(url, timeout=_TIMEOUT_S) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 0:
                logger.warning("[acousticid] rate-limited, backing off 1s")
                time.sleep(1.0)
                continue
            raise
    else:
        return None

    if data.get("status") != "ok":
        return None

    results = data.get("results", [])
    for r in results:
        score = float(r.get("score", 0))
        if score < 0.80:
            continue
        recordings = r.get("recordings", [])
        if recordings:
            return recordings[0]["id"], score

    return None


# ── Stage ─────────────────────────────────────────────────────────────────────


class AcousticIDStage(BaseStage):
    """
    AcousticID — fingerprint-based duplicate detection.
    Catches re-encodes that Sentinel's PCM hash misses.
    """

    NAME = "acousticid"

    def validate(self, ctx: RunContext) -> None:
        import shutil

        if not shutil.which("fpcalc"):
            raise StageError(
                "fpcalc not found in PATH. "
                "Install chromaprint: sudo apt install libchromaprint-tools"
            )
        api_key = ctx.config.acousticid_api_key
        if not api_key:
            logger.warning(
                "[acousticid] ACOUSTICID_API_KEY not set — fingerprints will be "
                "computed but not matched against AcousticID. "
                "Add key to ~/.config/musaeus/settings.env"
            )

        try:
            count = ctx.conn.execute(
                "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED' AND chromaprint IS NULL"
            ).fetchone()[0]
        except Exception:
            count = ctx.conn.execute(
                "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'"
            ).fetchone()[0]
        logger.info("[acousticid] %d file(s) need fingerprinting", count)

    def _run(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)

        if not dry_run:
            _ensure_columns(ctx.conn)

        api_key = ctx.config.acousticid_api_key

        try:
            rows = ctx.conn.execute(
                """
                SELECT file_path, duration FROM archive
                WHERE status = 'CATALOGUED'
                  AND chromaprint IS NULL
                ORDER BY file_path
                """
            ).fetchall()
        except Exception:
            rows = ctx.conn.execute(
                """
                SELECT file_path, duration FROM archive
                WHERE status = 'CATALOGUED'
                ORDER BY file_path
                """
            ).fetchall()

        # recording_id → [file_path] for dupe detection this run
        recording_map: dict[str, list[str]] = {}
        matched = 0
        dupes_found = 0

        from datetime import datetime, timezone

        now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

        for row in rows:
            result.files_processed += 1
            fp = row["file_path"]
            dur_db = row["duration"] or 0

            # Skip short clips
            if dur_db and float(dur_db) < _MIN_DURATION_S:
                result.files_skipped += 1
                continue

            # Compute fingerprint
            try:
                duration, fingerprint = _fpcalc(fp)
            except Exception as exc:
                logger.warning("[acousticid] fpcalc error %s: %s", fp, exc)
                result.files_skipped += 1
                result.errors.append(f"{fp}: {exc}")
                continue

            if duration < _MIN_DURATION_S:
                result.files_skipped += 1
                continue

            # Look up AcousticID
            recording_id: str | None = None
            score: float = 0.0
            if api_key:
                time.sleep(_RATE_LIMIT_S)
                try:
                    match = _acousticid_lookup(fingerprint, duration, api_key)
                    if match:
                        recording_id, score = match
                        matched += 1
                        logger.debug(
                            "[acousticid] %s → recording=%s score=%.2f",
                            fp,
                            recording_id,
                            score,
                        )
                except Exception as exc:
                    logger.warning("[acousticid] API error %s: %s", fp, exc)

            # Store results
            if not dry_run:
                ctx.conn.execute(
                    """
                    UPDATE archive
                       SET chromaprint=?,
                           chromaprint_duration=?,
                           acousticid_recording=?,
                           acousticid_score=?,
                           acousticid_checked_at=?
                     WHERE file_path=?
                    """,
                    (fingerprint, duration, recording_id, score if score else None, now, fp),
                )
                if recording_id:
                    ctx.log_event(
                        "ACOUSTIC_MATCHED",
                        file_path=fp,
                        new_value=recording_id,
                        stage=self.NAME,
                        note=f"score={score:.2f}",
                    )

            # Dupe detection within this run
            if recording_id:
                if recording_id not in recording_map:
                    # Also check DB for existing matches
                    try:
                        existing = ctx.conn.execute(
                            "SELECT file_path FROM archive "
                            "WHERE acousticid_recording=? AND file_path!=?",
                            (recording_id, fp),
                        ).fetchall()
                        recording_map[recording_id] = [r["file_path"] for r in existing]
                    except Exception:
                        recording_map[recording_id] = []

                if recording_map[recording_id]:
                    for other_fp in recording_map[recording_id]:
                        dupes_found += 1
                        group_id = f"acoustic_{uuid.uuid4().hex[:8]}"
                        logger.info(
                            "[acousticid] DUPE  %s  ==  %s  (recording=%s)",
                            fp,
                            other_fp,
                            recording_id,
                        )
                        if not dry_run:
                            for member in (fp, other_fp):
                                ctx.conn.execute(
                                    """
                                    INSERT OR IGNORE INTO duplicates
                                        (group_id, file_path, type, confidence,
                                         status, created_at)
                                    VALUES (?, ?, 'ACOUSTIC', ?, 'pending', ?)
                                    """,
                                    (group_id, member, score, now),
                                )
                            ctx.log_event(
                                "ACOUSTIC_DUPE_FOUND",
                                file_path=fp,
                                new_value=other_fp,
                                stage=self.NAME,
                                note=f"recording={recording_id}",
                            )
                        result.files_changed += 1

                recording_map[recording_id].append(fp)

            if result.files_processed % _COMMIT_EVERY == 0 and not dry_run:
                ctx.conn.commit()
                logger.info("[acousticid] checkpoint %d", result.files_processed)

        prefix = "Would fingerprint" if dry_run else "Fingerprinted"
        checked = result.files_processed - result.files_skipped
        result.notes.append(
            f"{prefix} {checked} file(s): {matched} AcousticID match(es), "
            f"{dupes_found} acoustic dupe(s) found."
        )
        if not api_key:
            result.notes.append(
                "ACOUSTICID_API_KEY not set — recording IDs not looked up. "
                "Fingerprints stored for future use."
            )
        if dupes_found:
            result.notes.append(f"{dupes_found} acoustic duplicate(s) staged → `musaeus dedupe`")

        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._run(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._run(ctx, dry_run=False)
