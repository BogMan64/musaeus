#!/usr/bin/env python3
"""
MUSAEUS — deep integrity scan: does every master still decode?

Two masters were found truncated on 2026-09-01 -- an intact container
header over missing audio, so a 5-minute song that decodes to 55 seconds.
Nothing in the pipeline was looking. They surfaced only because the Car
encoder verifies output duration against the source and refused to ship a
55-second track; before that they had sat in the library, reporting a
correct duration to anything that read metadata.

`CorruptStage.ffmpeg_decode_check(path, seconds=0)` was ported from ORPHEUS
the day before and could already answer the question. It was never called
from anywhere. This is the thing that calls it.

Why size alone is not the check
-------------------------------
Bytes-per-second flagged 418 files, of which 93 were lossless and 2 were
actually damaged. The other 91 were Bing Crosby, Count Basie, Billie
Holiday -- old mono recordings that genuinely compress to ~13% of PCM.
Acting on the ratio alone would have sent 91 undamaged masters for
re-sourcing.

So size is a PRIORITISER, not a verdict. Suspicious files are decoded
first because they are likelier to be damaged; everything else is decoded
too, just later. The verdict always comes from a decode.

Why it runs only when the machine is idle
-----------------------------------------
A full decode of 10,446 masters is many hours of CPU that nobody is
waiting on. It should not compete for the machine at all -- not run more
politely, but not exist while Grey is at the keyboard. It yields the
instant input arrives and resumes when the machine goes quiet, so it may
take days of wall clock to complete a pass. That is fine: it is looking
for damage that has already happened, and finding it a day later costs
nothing.

Resumability is therefore not a nicety. A scan that cannot resume would
never finish, because it will be interrupted constantly.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .db import ensure_columns as db_ensure_columns
from .idle_throttle import DEFAULT_IDLE_S, is_idle

logger = logging.getLogger(__name__)

#: Below this fraction of raw PCM for its sample rate, a lossless file is
#: unusually small and worth decoding sooner. NOT a verdict -- see above.
SUSPICIOUS_RATIO = 0.20

_LOSSLESS = ("alac", "flac", "wav", "aiff")


def ensure_columns(conn: sqlite3.Connection) -> None:
    """The columns this scan owns. Name kept: cli.py and corrupt.py import it."""
    db_ensure_columns(
        conn,
        (
            ("decode_checked_at", "TEXT"),
            ("decode_ok", "INTEGER"),
            ("decode_errors", "INTEGER"),
        ),
    )


def pcm_bytes_per_second(sample_rate: int | None) -> int:
    """16-bit stereo floor. Real 24-bit files are larger still, which only
    makes the ratio more conservative -- it under-flags rather than over."""
    return (sample_rate or 44100) * 2 * 2


def size_ratio(size_bytes: int | None, duration: float | None,
               sample_rate: int | None) -> float | None:
    """Stored size as a fraction of raw PCM. None when unknowable."""
    if not size_bytes or not duration or duration <= 0:
        return None
    return size_bytes / (pcm_bytes_per_second(sample_rate) * duration)


def looks_suspicious(row) -> bool:
    """Only meaningful for lossless: a lossy file is SUPPOSED to be small,
    so the ratio says nothing about it."""
    if (row["codec"] or "").lower() not in _LOSSLESS:
        return False
    r = size_ratio(row["size_bytes"], row["duration"], row["sample_rate"])
    return r is not None and r < SUSPICIOUS_RATIO


@dataclass
class ScanProgress:
    checked: int = 0
    corrupt: list[tuple[str, int]] = field(default_factory=list)
    yielded_to_user: int = 0
    stopped_reason: str = ""


def pending_rows(conn: sqlite3.Connection, *, limit: int | None = None) -> list:
    """Unchecked CATALOGUED rows, most-suspicious first.

    Ordering is the whole point of the size heuristic: a pass that will be
    interrupted repeatedly should spend its first minutes on the files most
    likely to be damaged, not on whatever sorts first alphabetically.
    """
    rows = conn.execute(
        """
        SELECT file_path, artist, title, duration, size_bytes, sample_rate, codec
          FROM archive
         WHERE status = 'CATALOGUED' AND decode_checked_at IS NULL
        """
    ).fetchall()
    rows.sort(key=lambda r: (
        not looks_suspicious(r),
        size_ratio(r["size_bytes"], r["duration"], r["sample_rate"]) or 1.0,
    ))
    return rows[:limit] if limit else rows


def scan(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    idle_only: bool = True,
    idle_threshold_s: float = DEFAULT_IDLE_S,
    decode_seconds: int = 0,
    on_result=None,
) -> ScanProgress:
    """Decode-verify pending masters, yielding the machine on input.

    `decode_seconds=0` decodes the whole file. ORPHEUS decoded only the
    first 10 seconds, which cannot see damage later in a track -- and both
    files found on 2026-09-01 were damaged well past the 10-second mark.
    """
    from .stages.corrupt import ffmpeg_decode_check

    ensure_columns(conn)
    conn.execute("PRAGMA busy_timeout = 60000")
    prog = ScanProgress()

    for row in pending_rows(conn, limit=limit):
        if idle_only and not is_idle(idle_threshold_s):
            # Wait rather than abandon: the machine being in use is the
            # normal state, not an error.
            prog.yielded_to_user += 1
            while not is_idle(idle_threshold_s):
                time.sleep(5)

        path = Path(row["file_path"])
        if not path.is_file():
            continue

        ok, detail = ffmpeg_decode_check(path, seconds=decode_seconds)
        n_err = 0 if ok else max(1, detail.count("\n") + 1)
        conn.execute(
            "UPDATE archive SET decode_checked_at = datetime('now'), "
            "decode_ok = ?, decode_errors = ? WHERE file_path = ?",
            (1 if ok else 0, n_err, str(path)),
        )
        conn.commit()

        prog.checked += 1
        if not ok:
            prog.corrupt.append((str(path), n_err))
            logger.warning("[deep-scan] CORRUPT %s — %s", path.name, detail[:120])
        if on_result:
            on_result(row, ok, n_err)

    prog.stopped_reason = "complete" if limit is None else f"limit {limit} reached"
    return prog
