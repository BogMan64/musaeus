#!/usr/bin/env python3
"""
MUSAEUS — Content-addressed audio hashing.

Key design decisions:
  - Hash only the AUDIO STREAM, not the full file.
    Tags (ID3, Vorbis Comments, APE) are irrelevant to identity.
    Re-tagging does NOT change a file's hash — this is intentional.

  - Implementation: ffmpeg/ffprobe extracts raw PCM packets; we hash those.
    This is stream-format agnostic: FLAC, MP3, AAC, OGG all work.

  - Fallback: full-file SHA-256 if ffmpeg is unavailable (logged as WARN).
    Full hash stored in archive.full_hash for change detection regardless.

  - Deterministic: same audio content → same hash, always.
    Different bit-depths / samplerates of "the same song" will produce
    different hashes — that's correct: they are different encodings.

Usage:
    from musaeus.hasher import audio_hash, file_hash
    h = audio_hash(Path("track.flac"))   # may raise HasherError
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_FFMPEG_CMD = "ffmpeg"
_FFPROBE_CMD = "ffprobe"
_READ_BYTES = 65536  # streaming chunk size


class HasherError(Exception):
    """Raised when hashing fails unrecoverably."""


# ── Internal helpers ──────────────────────────────────────────────────────────


def _probe_audio_meta(path: Path) -> tuple[int, float]:
    """Return (sample_rate_hz, duration_secs) for the first audio stream.

    Falls back to (48000, 0.0) on any probe failure so callers can still
    compute a conservative timeout rather than crashing.
    """
    cmd = [
        _FFPROBE_CMD,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=sample_rate:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        sample_rate = int(streams[0]["sample_rate"]) if streams else 48000
        duration = float(data.get("format", {}).get("duration", 0.0))
        return sample_rate, duration
    except Exception as exc:
        logger.debug("ffprobe metadata probe failed for %s: %s", path, exc)
        return 48000, 0.0


def _audio_hash_timeout(sample_rate: int, duration: float) -> int:
    """Compute a per-file ffmpeg timeout that scales with resolution and length.

    Baseline: 120 s covers any standard-rate (≤48 kHz) file up to ~10 min.
    High-res files (96/176.4/192 kHz) decode proportionally more samples,
    so the timeout scales linearly with sample_rate / 48000. A 30 s flat
    margin absorbs startup overhead and I/O jitter.
    """
    rate_factor = max(1.0, sample_rate / 48_000)
    return max(120, math.ceil(duration * rate_factor) + 30)


# ── Audio-stream hash ─────────────────────────────────────────────────────────


def audio_hash(path: Path) -> str:
    """
    Compute a SHA-256 hash of the raw audio stream only (no tags/container).

    Uses ffmpeg to decode to raw f32le PCM and hashes the stdout stream.
    This means re-tagging or re-muxing into a losslessly-equivalent container
    does not change the hash.

    Returns: lowercase hex string (64 chars)
    Raises: HasherError on subprocess failure or missing ffmpeg
    """
    cmd = [
        _FFMPEG_CMD,
        "-v",
        "error",  # suppress info noise
        "-i",
        str(path),
        "-vn",  # no video
        "-map",
        "0:a:0",  # first audio stream only
        "-c:a",
        "pcm_f32le",  # decode to raw 32-bit float PCM
        "-f",
        "data",  # raw binary output
        "pipe:1",  # stdout
    ]

    sample_rate, duration = _probe_audio_meta(path)
    _TIMEOUT_SECS = _audio_hash_timeout(sample_rate, duration)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise HasherError(f"ffmpeg not found — cannot compute audio hash for {path}") from exc

    import threading

    def _kill_on_timeout(p: subprocess.Popen, timeout: int) -> None:  # type: ignore[type-arg]
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg timed out after %ds for %s — killing", timeout, path)
            p.kill()

    timer = threading.Thread(target=_kill_on_timeout, args=(proc, _TIMEOUT_SECS), daemon=True)
    timer.start()

    h = hashlib.sha256()
    assert proc.stdout is not None
    while chunk := proc.stdout.read(_READ_BYTES):
        h.update(chunk)

    proc.stdout.close()
    rc = proc.wait()
    timer.join(timeout=1)

    if rc != 0:
        stderr = (
            proc.stderr.read().decode("utf-8", errors="replace").strip()
            if proc.stderr
            else ""
        )
        if rc == -9:  # SIGKILL from timeout
            raise HasherError(f"ffmpeg timed out (>{_TIMEOUT_SECS}s) for {path}")
        raise HasherError(f"ffmpeg exited {rc} for {path}: {stderr[:200]}")

    return h.hexdigest()


def audio_hash_safe(path: Path) -> tuple[str | None, str | None]:
    """
    Like audio_hash() but never raises.
    Returns (hash_str, None) on success, (None, error_msg) on failure.
    """
    try:
        return audio_hash(path), None
    except HasherError as exc:
        logger.warning("audio_hash failed for %s: %s", path, exc)
        return None, str(exc)


# ── Full-file hash ────────────────────────────────────────────────────────────


def file_hash(path: Path) -> str:
    """
    SHA-256 of the entire file (tags included).
    Used for change-detection: if file_hash changes but audio_hash doesn't,
    only the tags were modified.

    Returns: lowercase hex string (64 chars)
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_READ_BYTES):
            h.update(chunk)
    return h.hexdigest()


# ── Probe check ───────────────────────────────────────────────────────────────


def ffmpeg_available() -> bool:
    """Return True if ffmpeg is on PATH and functional."""
    try:
        result = subprocess.run(
            [_FFMPEG_CMD, "-version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def ffprobe_available() -> bool:
    """Return True if ffprobe is on PATH and functional."""
    try:
        result = subprocess.run(
            [_FFPROBE_CMD, "-version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
