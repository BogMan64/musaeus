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
import threading
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
        if result.returncode != 0 or not result.stdout.strip():
            logger.warning(
                "ffprobe probe returned rc=%d with empty stdout for %s",
                result.returncode,
                path,
            )
            return 48000, 0.0
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        sample_rate = int(streams[0]["sample_rate"]) if streams else 48000
        duration = float(data.get("format", {}).get("duration", 0.0))
        return sample_rate, duration
    except Exception as exc:
        logger.warning("ffprobe metadata probe failed for %s: %s", path, exc)
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

    # Hi-res audio used to be diverted to a full-file SHA-256 here, on the
    # assumption that decoding ≥96 kHz PCM "reliably exceeds any sane timeout
    # on spinning disk".
    #
    # Measured on this vault 2026-08-30, while a full re-hash was saturating
    # the same disk: five 192 kHz tracks of 3.5-5.5 minutes decoded in
    # 1.4-2.8 s each, against computed timeouts of 874-1334 s. Roughly 400x
    # headroom. The assumption does not hold here.
    #
    # It was not a cheap assumption. A full-file hash covers the CONTAINER,
    # so it changes when tags change -- which breaks this module's own
    # contract, stated in sentinel.py: "re-tagging a file: full_hash changes,
    # audio_hash unchanged → NO duplicate". It also cannot match the same
    # audio in a different container, which is the entire point of hashing
    # the stream. 3,975 of the first 8,500 rows in that re-hash -- 47% -- were
    # taking this path.
    #
    # So: attempt the real decode, and fall back only on an ACTUAL timeout,
    # not on a prediction of one. The timeout already scales with sample rate
    # (see _audio_hash_timeout), so a genuinely slow disk still degrades
    # gracefully rather than failing.
    if sample_rate >= 96000:
        logger.debug("hi-res audio (%d Hz), decoding normally: %s", sample_rate, path.name)

    _TIMEOUT_SECS = _audio_hash_timeout(sample_rate, duration)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise HasherError(f"ffmpeg not found — cannot compute audio hash for {path}") from exc

    def _kill_on_timeout(p: subprocess.Popen, timeout: int) -> None:  # type: ignore[type-arg]
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg timed out after %ds for %s — killing", timeout, path)
            p.kill()

    timer = threading.Thread(target=_kill_on_timeout, args=(proc, _TIMEOUT_SECS), daemon=True)
    timer.start()

    # stderr MUST be drained while stdout is being read.
    #
    # Both are pipes. Reading stdout to exhaustion and only then reading
    # stderr deadlocks the moment ffmpeg writes more than the pipe buffer
    # (~64 KB on Linux) to stderr: the child blocks writing, so it stops
    # producing stdout, so this loop never ends and proc.wait() never
    # returns. ffmpeg is verbose on stderr and gets more so on a damaged
    # file -- precisely when hashing matters.
    #
    # Reproduced 2026-08-30 against a child emitting 2 MB of stderr: the
    # old order was still blocked after 20s, with only the kill-timer
    # eventually freeing it, and that timer scales with track duration.
    stderr_chunks: list[bytes] = []

    def _drain_stderr() -> None:
        if proc.stderr is None:
            return
        try:
            for line in iter(lambda: proc.stderr.read(_READ_BYTES), b""):  # type: ignore[union-attr]
                # Bounded: a runaway child must not be able to exhaust
                # memory through the error path either.
                if len(stderr_chunks) < 64:
                    stderr_chunks.append(line)
        except (OSError, ValueError):
            pass  # closed underneath us; the exit code still tells the story

    drainer = threading.Thread(target=_drain_stderr, daemon=True)
    drainer.start()

    h = hashlib.sha256()
    assert proc.stdout is not None
    try:
        while chunk := proc.stdout.read(_READ_BYTES):
            h.update(chunk)
    finally:
        proc.stdout.close()

    rc = proc.wait()
    drainer.join(timeout=5)
    timer.join(timeout=1)

    # Closed explicitly: Popen only closes these on garbage collection, and
    # this runs once per file across tens of thousands of files.
    if proc.stderr is not None:
        proc.stderr.close()

    if rc != 0:
        stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
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
