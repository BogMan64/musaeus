#!/usr/bin/env python3
"""
How long is this file, who is answering, and can they know.

The problem
-----------
There are three answers and they are not interchangeable.

The CONTAINER header carries a duration. The AUDIO STREAM carries one too.
In MP4/M4A -- which is every file in this library -- BOTH live in the same
moov atom, so both are written before the audio and both survive the audio
being cut off. Measured 2026-09-02 on a deliberately truncated 30 s file
with the header intact at the front: container said 30.0, stream said
30.0, and 409 AAC frames actually decoded, about 9.5 seconds.

So metadata cannot detect truncation here, and an earlier draft of this
very module claimed the stream was "the honest one". It is not. Only
DECODING is.

And decoding says so on stderr, not through its exit status: that same
truncated file makes ffmpeg print "Input buffer exhausted before END
element found" and "partial file" -- and exit 0. A check that trusts the
return code sees a clean decode. orpheus_noise_generator's
_decodes_cleanly() gets this right (returncode == 0 AND empty stderr);
anything reading only the exit code does not.

Why this module exists
----------------------
On 2026-09-02 there were seven implementations of "read the duration",
split four to two between container and stream, with canonicalize.py using
both in one file, and nothing naming the distinction -- so each site got
whichever the author happened to type.

corrupt.py, the corruption detector, asked the stream and silently fell
back to the container under a docstring promising "actual decoded
duration". For the one file shape it exists to catch, both of its answers
come from the same header, and its caller had no way to tell which it got
or that neither could see the problem.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

#: How far a duration may drift before it means something.
#:
#: Container and codec rounding, and AAC encoder priming, shift a reported
#: duration slightly even on a correct encode -- so a tolerance of zero
#: cries wolf on working files, and verification that cries wolf gets
#: switched off.
#:
#: Was 1.5 in four places and 2.0 in a fifth, whose comment cited "the same
#: rationale" as one of the 1.5s. Same stated reasoning, different number,
#: nobody deciding. Grey's ruling 2026-09-02: 2.0 everywhere.
TOLERANCE_SEC = 2.0


def tolerance_for(recorded_sec: float | None) -> float:
    """How far a duration of this length may drift before it means something.

    TOLERANCE_SEC is a floor, not the whole answer: a flat 2s is right for a
    short track and far too strict for a long one, where container rounding
    and encoder padding scale with length. 2% of a 5-minute track is 6s.

    This existed as a bare `max(2.0, recorded * 0.02)` inside
    canonicalize.verify_effect -- a sixth copy of a constant the repo's
    CLAUDE.md already lists as recurring ("5 copies, 1.5 four times, 2.0
    once, same stated rationale"), in a file that already imports
    TOLERANCE_SEC twelve lines above. Named here so changing the rule
    changes it everywhere, which is the whole point of the entry.
    """
    if not recorded_sec or recorded_sec <= 0:
        return TOLERANCE_SEC
    return max(TOLERANCE_SEC, recorded_sec * 0.02)

_TIMEOUT_S = 15
#: A full decode of a long track is minutes, not seconds.
_DECODE_TIMEOUT_S = 900

#: Which measurement a duration came from. A caller doing corruption work
#: should treat CONTAINER as "unverified" rather than as an answer.
STREAM = "stream"
CONTAINER = "container"


def _probe(args: list[str], path: Path) -> dict | None:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", *args, "-of", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def stream_seconds(path: Path) -> float | None:
    """The audio stream's declared duration. Metadata, not measurement.

    In MP4 this comes from the same moov atom as container_seconds and is
    equally blind to truncation. Useful where the two genuinely differ
    (some containers), never as proof a file is intact.
    """
    data = _probe(["-select_streams", "a:0", "-show_entries", "stream=duration"], path)
    if not data:
        return None
    streams = data.get("streams") or []
    if not streams:
        return None
    raw = streams[0].get("duration")
    try:
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None


def container_seconds(path: Path) -> float | None:
    """What the container header claims. Metadata, not measurement.

    Right for scaling a timeout or totalling a library. Never evidence
    that the audio behind it is all there.
    """
    data = _probe(["-show_entries", "format=duration"], path)
    if not data:
        return None
    raw = (data.get("format") or {}).get("duration")
    try:
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None


def duration_with_source(path: Path) -> tuple[float | None, str | None]:
    """Prefer the stream, fall back to the container, and SAY WHICH.

    The fallback is pragmatic -- some containers genuinely carry no
    stream-level duration, and refusing to answer would skip the check
    entirely. What is not acceptable is the caller being unable to tell.
    Returns (seconds, STREAM|CONTAINER), or (None, None).
    """
    s = stream_seconds(path)
    if s is not None:
        return s, STREAM
    c = container_seconds(path)
    if c is not None:
        return c, CONTAINER
    return None, None


def decodes_cleanly(path: Path) -> tuple[bool, str | None]:
    """Decode the whole file and report whether ffmpeg found it intact.

    The only check here that can detect truncation, because it is the only
    one that reads past the header.

    Returns (ok, first_error). NOT just the exit status: a file truncated
    mid-mdat decodes with "Input buffer exhausted before END element found"
    and "partial file" on stderr and STILL EXITS 0. Trusting the return
    code is how a truncated file passes a corruption check.

    Costs a full decode, so sample rather than sweep.
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostats", "-i", str(path), "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=_DECODE_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return False, str(exc)
    stderr = (r.stderr or "").strip()
    if r.returncode != 0 or stderr:
        first = stderr.splitlines()[0] if stderr else f"ffmpeg exited {r.returncode}"
        return False, first
    return True, None
