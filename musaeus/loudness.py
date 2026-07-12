#!/usr/bin/env python3
"""
MUSAEUS — Loudness measurement module.

Measures integrated loudness (EBU R128) via ffmpeg loudnorm.
Returns LUFS + true-peak; never re-encodes audio.

Reference: -18 LUFS (home listening middle-ground)
  Apple Music target: -16 LUFS
  Spotify target:     -14 LUFS
  EBU R128 broadcast: -23 LUFS
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

R128_REFERENCE: float = -18.0        # ReplayGain 2 target for FLAC/MP3 tags
R128_APPLE_REFERENCE: float = -23.0  # EBU R128 reference; required by Apple R128_TRACK_GAIN
_SILENCE_FLOOR: float = -70.0        # below this → treat as silence/unmeasurable

# Per-file ffmpeg timeout: cap at 600s for very long files (30s minimum)
_TIMEOUT_MIN  = 30
_TIMEOUT_MAX  = 600
_DURATION_FRAC = 0.3   # 30 % of file duration


# ── ffmpeg helpers ────────────────────────────────────────────────────────────

def _get_duration(path: Path) -> float | None:
    """Quick ffprobe duration check (used to scale the timeout)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_entries", "format=duration", str(path)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception as exc:
        logger.debug("_get_duration failed for %s: %s", path, exc)
        return None


def _calc_timeout(duration: float | None) -> int:
    if duration is None:
        return 120
    return int(min(_TIMEOUT_MAX, max(_TIMEOUT_MIN, duration * _DURATION_FRAC)))


def _kill_on_timeout(proc: subprocess.Popen, secs: int, path: Path) -> threading.Timer:
    """Return a started timer that kills *proc* after *secs* seconds."""
    def _kill() -> None:
        try:
            proc.kill()
        except OSError:
            pass
        logger.warning("ffmpeg killed after %ds timeout: %s", secs, path)

    t = threading.Timer(secs, _kill)
    t.daemon = True
    t.start()
    return t


def _extract_loudnorm_json(stderr: str) -> dict | None:
    """
    Robustly extract the loudnorm JSON block from ffmpeg stderr.
    Strategy 1: scan all non-nested {…} blocks in reverse, pick one with 'input_i'.
    Strategy 2: slice from last { to last } and parse.
    """
    for m in reversed(list(re.finditer(r"\{[^{}]*\}", stderr, re.DOTALL))):
        candidate = m.group().strip()
        try:
            data = json.loads(candidate)
            if "input_i" in data:
                return data
        except json.JSONDecodeError:
            continue

    lo, hi = stderr.rfind("{"), stderr.rfind("}")
    if lo != -1 and hi != -1 and hi > lo:
        try:
            data = json.loads(stderr[lo : hi + 1].strip())
            if "input_i" in data:
                return data
        except json.JSONDecodeError:
            pass

    return None


def _run_loudnorm(path: Path, linear: bool, duration: float | None) -> tuple[int, str]:
    """
    Run one ffmpeg loudnorm pass.  Returns (returncode, stderr).
    Uses a threading.Timer to kill runaway processes.
    """
    af = "loudnorm=print_format=json"
    if linear:
        af += ":linear=true"

    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(path),
        "-map", "0:a",
        "-af", af,
        "-f", "null", "-",
    ]
    timeout = _calc_timeout(duration)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    timer = _kill_on_timeout(proc, timeout, path)
    try:
        _, stderr_bytes = proc.communicate()
    finally:
        timer.cancel()

    return proc.returncode, (stderr_bytes or b"").decode("utf-8", "replace")


# ── Public API ────────────────────────────────────────────────────────────────

def measure_loudness(path: Path) -> tuple[float | None, float | None, str]:
    """
    Measure integrated loudness of *path* using ffmpeg loudnorm.

    Returns (lufs, true_peak_dbtp, reason):
      reason == "ok"           — success
      reason == "silence"      — audio present but below measurable floor
      reason == "json_fail"    — ffmpeg ran but JSON missing (corrupted output)
      reason == "ffmpeg_fail"  — subprocess / timeout error
      reason == "missing"      — file does not exist
    """
    if not path.exists():
        return None, None, "missing"

    try:
        duration = _get_duration(path)

        # Attempt 1: dynamic mode
        rc, stderr = _run_loudnorm(path, linear=False, duration=duration)
        data = _extract_loudnorm_json(stderr)

        # Attempt 2: linear mode (more tolerant of short/odd files)
        if data is None:
            logger.debug("loudnorm dynamic attempt failed (rc=%d), retrying linear: %s", rc, path)
            rc, stderr = _run_loudnorm(path, linear=True, duration=duration)
            data = _extract_loudnorm_json(stderr)

        if data is None:
            logger.warning("loudnorm JSON parse failed: %s", path)
            return None, None, "json_fail"

        lufs = float(data.get("input_i", -999))
        tp   = float(data.get("input_tp", -100))

        if lufs < _SILENCE_FLOOR:
            return lufs, tp, "silence"

        return lufs, tp, "ok"

    except Exception as exc:
        logger.warning("loudness measure error for %s: %s", path, exc)
        return None, None, "ffmpeg_fail"


def lufs_to_rg(lufs: float, reference: float = R128_REFERENCE) -> float:
    """Convert measured LUFS to a ReplayGain track gain (dB)."""
    return reference - lufs


def dbtp_to_linear(dbtp: float) -> float:
    """Convert true-peak dBTP to a linear ratio."""
    return 10 ** (dbtp / 20.0)
