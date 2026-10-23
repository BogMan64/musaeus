#!/usr/bin/env python3
"""
MUSAEUS — Corruption Detection Stage

Detects truncated or corrupt audio files via ffprobe analysis.

What it does:
  1. Checks filesize vs duration ratio (codec-appropriate thresholds)
  2. Flags suspiciously short tracks (<45s) unless they match keywords
  3. Quarantines corrupt files to QUARANTINE/corrupted/ on a real
     run. There is no --apply flag and there never was; a dry run is
     what reports without moving. (P0-G, 2026-09-09.)
  4. Reports findings to validation_issues table

Based on ORPHEUS orpheus_corrupt_detector.py
"""

from __future__ import annotations

import logging
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ..context import StageResult, elision
from ..deep_scan import ensure_columns as deep_scan_ensure_columns
from ..doctor import song_key
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

# A file this short is not a recording at all, whatever it calls itself. No
# keyword exempts it: an "intro" of one second is still not an intro.
NEAR_ZERO_SEC = 3.0

# The RELATIVE rule, ported from doctor 2026-09-07 on Grey's instruction.
#
# MIN_DURATION_SEC is absolute, and absolute floors cannot tell a fragment
# from a short song. Measured against the 22 fragments doctor found: this
# stage caught 13 and missed 9 -- five for being over 45s (Def Leppard 48s
# beside a 4:27 copy, Player 56s, Olivia Newton-John 57s, Mr. Mister 59s)
# and four to keyword exemptions (Elvis "Can't Help Falling in Love (Epic
# intro)" at 21s beside a 2:57 copy).
#
# Raising the floor is not the fix. Measured on the live library, a 120s
# floor flags 397 complete recordings -- "Hit the Road Jack" (2:00), "All
# Shook Up" (1:58), "It's Not Unusual" (2:00). Early rock and 60s pop are
# supposed to be short.
#
# What separates a fragment from a short song is length relative to ANOTHER
# COPY OF THE SAME RECORDING. A 21-second "Cruel Summer" is wrong because a
# 179-second one sits beside it; a 33-second "Bookends Theme" is fine
# because the longest copy is 83 seconds.
FRAGMENT_MAX_SEC = 60.0
FRAGMENT_SIBLING_MIN_SEC = 120.0
assert FRAGMENT_MAX_SEC * 2 <= FRAGMENT_SIBLING_MIN_SEC, (
    "a flagged file must be at most half its sibling; adjust both bounds "
    "together or restore an explicit ratio test"
)

# Short-track keywords — these are allowed to be short
SHORT_OK_KEYWORDS = re.compile(
    r"\b(intro|outro|skit|reprise|interlude|snippet|clip|"
    r"bonus|fragment|excerpt|medley|bridge)\b",
    re.IGNORECASE,
)


def _longest_sibling(row, longest_by_song: dict) -> float:
    """The longest OTHER copy of this recording, 0 if this row IS the longest.

    Returning this row's own duration would make every file its own sibling
    and flag nothing, which is the quiet way this check could stop working.
    """
    longest = longest_by_song.get(song_key(row["artist"], row["title"]), 0.0)
    return 0.0 if longest <= (row["duration"] or 0.0) else longest


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


def check_file(
    path: Path,
    codec: str | None,
    duration_db: float | None,
    longest_sibling_sec: float = 0.0,
    title: str | None = None,
) -> tuple[bool, str]:
    """
    Check if file is corrupt based on size/duration ratio.
    Returns (is_suspect, reason).

    *longest_sibling_sec* is the duration of the longest OTHER copy of the
    same recording, or 0 when the caller has no library to compare against.
    Passed in rather than looked up so this stays a pure function of its
    arguments -- the caller already holds every row.
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

    # Check 2: not a recording at all. Deliberately BEFORE the keyword
    # exemption -- nothing legitimate is under three seconds, and ORPHEUS's
    # equivalent floor of `< 1.0` let a 1-second file through on the boundary.
    if declared_sec < NEAR_ZERO_SEC:
        return True, f"duration {declared_sec:.1f}s -- not a recording"

    # Check 3: suspiciously short duration (absolute)
    if declared_sec < MIN_DURATION_SEC:
        # The row's title first, the filename second. Reading only path.stem
        # worked for organised files -- MUSAEUS names them "Artist - Title" --
        # and failed for anything still carrying its arrival name. A track
        # called "01 - track01.m4a" whose title is "Intro" lost its exemption
        # and was physically moved to QUARANTINE. Searching both keeps the
        # organised case working and stops the un-organised one being punished
        # for a filename it has not been given yet. (P0-G, 2026-09-09.)
        haystack = f"{title or ''} {path.stem}"
        if not SHORT_OK_KEYWORDS.search(haystack):
            reason = f"duration {declared_sec:.0f}s suspiciously short"
            return True, reason

    # Check 4: short RELATIVE to another copy of the same recording.
    #
    # No keyword exemption here, and that is the point. The exemptions above
    # fire on the title alone, so "(Epic intro)" excused a 21-second Elvis
    # clip that has a 2:57 copy beside it. A much longer copy of the same
    # recording is evidence the title cannot override.
    if declared_sec < FRAGMENT_MAX_SEC and longest_sibling_sec >= FRAGMENT_SIBLING_MIN_SEC:
        return True, (
            f"duration {declared_sec:.0f}s, but another copy of this "
            f"recording runs {longest_sibling_sec:.0f}s -- likely a preview clip"
        )

    return False, ""


# An .m4a carries its cover art as a second, VIDEO stream, and `ffmpeg -i
# file -f null -` decodes every stream in the container, not just the audio.
# So a file with broken artwork and perfect audio still writes decoder errors
# to stderr. `-vn` stops ffmpeg DECODING the artwork, which removes most of
# them, and it is in the command below. It cannot remove all of them: the
# mjpeg header is parsed at demux time, before stream selection applies, so
# two lines survive `-vn` and plain `ffprobe` prints them too. Hence both a
# narrower command and a classifier -- neither alone is enough.
#
# Measured 2026-09-08 on `Andy Gibb - (Love Is) Thicker Than Water` and
# `Baltimora - Tarzan Boy`: both decode their full ALAC stream with exit 0,
# and both were reported as damaged solely on `[mjpeg @ ...] unable to decode
# APP fields`. They are the regression fixtures for this function.
#
# This is a DENYLIST -- drop lines known to come from an image decoder, keep
# everything else -- and the direction matters. An unrecognised image codec
# leaks through as a false positive, which lands in the review CSV and gets a
# ruling. An allowlist of known audio errors would fail the other way: an
# unrecognised audio error would be dropped and a damaged master would be
# baked into an edition. Fail towards the human, not towards the encoder.
_IMAGE_DECODER_TAGS = frozenset(
    {
        "mjpeg",
        "png",
        "bmp",
        "gif",
        "tiff",
        "webp",
        "image2",
        "swscaler",
    }
)
_TAGGED_STDERR_RE = re.compile(r"^\[(?P<tag>[A-Za-z0-9_]+) @ 0x[0-9a-f]+\]")
_STREAM_DECODE_ERROR_RE = re.compile(r"^Error while decoding stream #\d+:(?P<idx>\d+)")
# A muxer throughput complaint, not a statement about the audio. It only
# appears without `-vn`; the rule is here so the classifier still holds if
# someone later changes the command.
_PACKETS_BUFFERED_RE = re.compile(r"^Too many packets buffered for output stream \d+:(?P<idx>\d+)")


def audio_stream_index(path: Path) -> int | None:
    """The container index of the first audio stream, or None if unknown.

    Needed because ffmpeg's `Error while decoding stream #0:1` lines carry no
    bracketed decoder tag, so they cannot be attributed by name -- only by
    which stream they name. On these files #0:1 is the artwork.
    """
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    raw = (proc.stdout or "").strip().splitlines()
    if not raw:
        return None
    try:
        return int(raw[0].strip().rstrip(","))
    except ValueError:
        return None


def audio_relevant_stderr(stderr: str, audio_index: int | None) -> str:
    """Strip the lines that describe the artwork, keep the ones about audio.

    Pure and side-effect free on purpose: the classification is what is worth
    testing, and it can be tested without ffmpeg or a real damaged file.
    """
    kept = []
    dropped_last = False
    for line in (stderr or "").splitlines():
        if not line.strip():
            continue
        # "Last message repeated N times" repeats the line before it, and
        # shares its fate: after a cover-art error it is about the picture.
        # Kept, it reported a broken jpeg as damaged audio (Foreigner,
        # 2026-09-26).
        if line.strip().startswith("Last message repeated"):
            if not dropped_last:
                kept.append(line)
            continue
        dropped_last = False
        tagged = _TAGGED_STDERR_RE.match(line)
        if tagged and tagged.group("tag").lower() in _IMAGE_DECODER_TAGS:
            dropped_last = True
            continue
        streamed = _STREAM_DECODE_ERROR_RE.match(line) or _PACKETS_BUFFERED_RE.match(line)
        # `audio_index` is an INPUT stream index and these lines name an
        # OUTPUT one. With no -map they agree, and where they do not the
        # mismatch keeps the line rather than dropping it -- a false
        # positive a human rules on, not a silent acquittal.
        if streamed and audio_index is not None and int(streamed.group("idx")) != audio_index:
            continue
        kept.append(line)
    return "\n".join(kept)


class DecodeUnavailable(RuntimeError):
    """The decode could not be done -- ffmpeg missing or killed, a timeout, the
    disk failing -- so nothing is known about the file.

    Review of slice B, finding 1: these came back as (False, reason), the same
    as damage, and CorruptStage moved healthy masters to QUARANTINE and
    reported success. A machine failure is not a verdict on the file (the
    rule Sentinel's _is_environmental already applies)."""


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

    Cover-art errors are not audio errors -- see the note above
    `audio_relevant_stderr`. Within DEFAULT_PIPELINE this stage runs BEFORE
    `albumart`, so the files it sees usually carry no artwork yet and the
    distinction never arose; it surfaced when scripts/decode_audit.py became
    the first thing to decode the FINISHED library.

    Returns (False, reason) only when ffmpeg decoded the file and found
    damage. Raises DecodeUnavailable when it could not decode at all.
    """
    # -nostdin: without it ffmpeg reads the terminal, and a 'q' typed during
    # a run ended the decode early with exit 0 -- a damaged file read as clean
    # (review of slice B, finding 2; the hasher learned this in 87-6).
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path)]
    if seconds:
        cmd += ["-t", str(seconds)]
    # -vn: decode the audio, not the cover art. Without it a file with broken
    # artwork also emits "Error while decoding stream #0:1" and "Too many
    # packets buffered for output stream 0:1", neither of which is a fact
    # about the audio.
    cmd += ["-vn", "-f", "null", "-"]
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_DECODE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise DecodeUnavailable(f"decode timed out after {_DECODE_TIMEOUT_S}s") from None
    except OSError as exc:
        raise DecodeUnavailable(f"ffmpeg unavailable: {exc}") from None
    raw = (proc.stderr or "").strip()
    # Stopped, not finished: killed (negative) or SIGTERM/SIGINT, which ffmpeg
    # catches and exits 255 from, usually silently. Asked before anything in
    # stderr, so a line about the audio cannot hide it (pre-merge review of
    # #162).
    if proc.returncode < 0 or proc.returncode == 255:
        raise DecodeUnavailable(f"ffmpeg was stopped (exit {proc.returncode})")
    if raw:
        from .sentinel import _is_environmental

        # Asked of the WHOLE message, with the file's own path taken out:
        # ffmpeg puts the path before "Input/output error", so cutting to 200
        # characters first lost the marker on long paths, and a file NAMED
        # "Permission Denied.m4a" must not read as the machine's failure.
        scrubbed = raw.replace(str(path), "<file>")
        if _is_environmental(scrubbed):
            raise DecodeUnavailable(scrubbed[-200:])
    # Only pay for the extra ffprobe when there is something to classify.
    detail = audio_relevant_stderr(raw, audio_stream_index(path)) if raw else ""
    if proc.returncode != 0 or detail:
        return False, (detail or f"ffmpeg exited {proc.returncode}").strip()[:200]
    return True, ""


def _quarantine_dest(
    conn: sqlite3.Connection,
    quarantine_dir: Path,
    src: Path,
    reserved: set[Path] | None = None,
) -> Path:
    """A name in quarantine that is free on disk AND in the catalogue.

    Review of slice B, finding 5: only the disk was asked, so an old DELETED
    or QUARANTINED row still holding the name made the row's UPDATE fail
    (file_path is UNIQUE) after the file had already moved.
    """
    dest = quarantine_dir / src.name
    n = 1
    while (
        dest.exists()
        or (reserved is not None and dest in reserved)
        or conn.execute("SELECT 1 FROM archive WHERE file_path = ?", (str(dest),)).fetchone()
    ):
        dest = quarantine_dir / f"{src.stem}_{n}{src.suffix}"
        n += 1
    return dest


class CorruptStage(BaseStage):
    """Detect and quarantine corrupt/truncated audio files."""

    NAME = "corrupt"

    #: Bound on how many never-checked, non-suspect files get decoded per
    #: run for the "right-sized but damaged" class check_file() cannot see
    #: at all. Overridable per-instance for tests. See the comment at its
    #: use site in _scan for why this exists and how the number was picked.
    NEW_ARRIVAL_DECODE_BUDGET = 200

    #: plan_actions reads and decodes audio: only `musaeus plan --deep` asks it.
    PLAN_ACTIONS_DEEP = True

    @classmethod
    def plan_actions(cls, conn, cfg) -> tuple[list[dict[str, str]], str]:
        """Decode the same current candidates as the run, without recording verdicts.

        This can take minutes when the new-arrival decode budget is full.
        Files catalogued later in the pipeline cannot be predicted here.
        """
        cols = {r[1] for r in conn.execute("PRAGMA table_info(archive)")}
        decode_cols = (
            "decode_ok, decode_checked_at"
            if {"decode_ok", "decode_checked_at"} <= cols
            else "NULL AS decode_ok, NULL AS decode_checked_at"
        )
        rows = conn.execute(
            f"SELECT file_path, codec, duration, artist, title, {decode_cols} "
            "FROM archive WHERE status = 'CATALOGUED' AND file_path IS NOT NULL "
            "ORDER BY artist, album"
        ).fetchall()
        longest: dict[tuple[str, str], float] = {}
        for row in rows:
            if row["duration"]:
                key = song_key(row["artist"], row["title"])
                longest[key] = max(longest.get(key, 0.0), row["duration"])
        quarantine_dir = cfg.quarantine / "corrupted"
        reserved: set[Path] = set()
        actions: list[dict[str, str]] = []
        unchecked_decodes = 0
        undecodable = 0
        for row in rows:
            source = Path(row["file_path"])
            if not source.exists():
                continue
            suspect, reason = check_file(
                source,
                row["codec"],
                row["duration"],
                _longest_sibling(row, longest),
                title=row["title"],
            )
            should_decode = suspect or row["decode_ok"] == 0
            if row["decode_ok"] == 0 and not suspect:
                reason = "marked undecodable on an earlier decode"
            if (
                not suspect
                and row["decode_checked_at"] is None
                and unchecked_decodes < cls.NEW_ARRIVAL_DECODE_BUDGET
            ):
                should_decode = True
                unchecked_decodes += 1
            if not should_decode:
                continue
            try:
                decoded_ok, decode_err = ffmpeg_decode_check(source, seconds=0)
            except DecodeUnavailable:
                undecodable += 1
                continue
            if decoded_ok:
                continue
            reason = (
                f"{reason}; decode failed: {decode_err}"
                if suspect or row["decode_ok"] == 0
                else f"decode failed: {decode_err}"
            )
            target = _quarantine_dest(conn, quarantine_dir, source, reserved)
            reserved.add(target)
            actions.append(
                {
                    "source": str(source),
                    "destination": str(target),
                    "reason": reason,
                    "action": "quarantine",
                }
            )
        description = "current files confirmed corrupt by full audio decode"
        if undecodable:
            description += f"; {undecodable} could not be decoded and remain unknown"
        return actions, description

    @classmethod
    def plan_candidates(cls, conn, cfg) -> tuple[int, str]:
        """Rows this stage would act on. Read-only; see planner.py.

        Mirrors _scan's own WHERE clause rather than validate's, because the
        preview must answer for what the stage *does*: validate counts every
        CATALOGUED row, _scan skips those with a NULL file_path. Two counts
        that disagree about the same stage is the failure this codebase keeps
        finding, so there is one query and _scan owns its shape.
        """
        n = conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status = 'CATALOGUED' AND file_path IS NOT NULL"
        ).fetchone()[0]
        return int(n), (
            f"CATALOGUED tracks to scan; quarantine destinations are unknown "
            f"until audio decoding (up to {cls.NEW_ARRIVAL_DECODE_BUDGET} "
            "never-checked files per run; `musaeus plan --deep` decodes them now)"
        )

    def validate(self, ctx: RunContext) -> None:
        count = ctx.conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'"
        ).fetchone()[0]
        logger.info("[corrupt] %d CATALOGUED tracks to scan", count)

    def _scan(self, ctx: RunContext, dry_run: bool) -> StageResult:
        """Scan for corrupt files, optionally quarantine them."""
        result = self._make_result(dry_run=dry_run)
        conn = ctx.conn
        # The decode columns are part of the schema (open_db adds them) since
        # Finalize reads them; this stays for a connection opened some other
        # way. A dry run never adds a column (a preview must not alter
        # schema), so the columns are selected only if they are there; NULL
        # stands in otherwise, which is exactly what "never checked" means.
        if not dry_run:
            deep_scan_ensure_columns(conn)

        _has_decode_cols = {"decode_ok", "decode_checked_at"} <= {
            r[1] for r in conn.execute("PRAGMA table_info(archive)").fetchall()
        }
        decode_cols_sql = (
            "decode_ok, decode_checked_at"
            if _has_decode_cols
            else "NULL AS decode_ok, NULL AS decode_checked_at"
        )

        # Get all CATALOGUED tracks with file specs
        rows = conn.execute(
            f"""
            SELECT file_path, codec, duration, artist, title, {decode_cols_sql}
            FROM archive
            WHERE status = 'CATALOGUED'
              AND file_path IS NOT NULL
            ORDER BY artist, album
            """
        ).fetchall()

        # Longest duration per recording, for check 4. Built once from the
        # rows already fetched -- song_key is doctor's, imported rather than
        # reimplemented so the two cannot disagree about what "the same
        # recording" means.
        longest_by_song: dict[tuple[str, str], float] = {}
        for _r in rows:
            if not _r["duration"]:
                continue
            _k = song_key(_r["artist"], _r["title"])
            if _r["duration"] > longest_by_song.get(_k, 0.0):
                longest_by_song[_k] = _r["duration"]

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
        # large backlog of MASTERS is left for deep_scan rather than blocking
        # here. New arrivals are not budgeted: _gate_new_arrivals decodes
        # every one (~1.5 s/file), since Finalize files none that has not
        # decoded clean.
        new_arrival_decodes = 0
        # The configured quarantine (MUSAEUS_QUARANTINE), which verify_effect
        # checks against -- not vault_root/QUARANTINE, which it only is by
        # default (Grey's code review, 2026-10-11, finding 9).
        quarantine_dir = ctx.quarantine / "corrupted"

        for row in rows:
            result.files_processed += 1
            file_path = Path(row["file_path"])

            if not file_path.exists():
                result.files_skipped += 1
                continue

            is_suspect, reason = check_file(
                file_path,
                row["codec"],
                row["duration"],
                _longest_sibling(row, longest_by_song),
                title=row["title"],
            )

            # The size ratio is a PRIORITISER, not a verdict -- deep_scan.py
            # says so and has the numbers: it flagged 418 files, of which 2
            # were damaged and 91 were undamaged Bing Crosby / Count Basie
            # mono recordings that genuinely compress to ~13% of PCM.
            #
            # This stage MOVES files to QUARANTINE. Acting on the ratio
            # alone meant physically removing ~91 good masters on a
            # heuristic that was right about 0.5% of the time.
            should_decode = is_suspect
            # Already marked undecodable -- by this stage on a prior run, or
            # by deep_scan, which shares these columns. Decoded again before
            # anything moves: the mark may have been the machine's failure,
            # not the file's (review of slice B, finding 1), and this stage
            # MOVES files.
            if row["decode_ok"] == 0:
                should_decode = True
                if not is_suspect:
                    reason = "marked undecodable on an earlier decode"
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
                try:
                    decoded_ok, decode_err = ffmpeg_decode_check(file_path, seconds=0)
                except DecodeUnavailable as exc:
                    # Not checked: nothing recorded, nothing moved, and the
                    # run is not reported clean.
                    result.files_errored += 1
                    result.errors.append(f"{file_path.name}: not decoded: {exc}")
                    continue
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
                reason = (
                    f"{reason}; decode failed: {decode_err}"
                    if is_suspect or row["decode_ok"] == 0
                    else (f"decode failed: {decode_err}")
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
                    dest = _quarantine_dest(conn, quarantine_dir, file_path)

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
                        try:
                            conn.execute(
                                "UPDATE archive SET status='QUARANTINED', "
                                "file_path=? WHERE file_path=?",
                                (str(dest), str(file_path)),
                            )
                            # Under the NEW path, where the row now is:
                            # verify_effect joins on it (review of slice B,
                            # finding 3 -- CORRUPT_DETECTED names the old path,
                            # so the check never found a row).
                            ctx.log_event(
                                "QUARANTINE",
                                file_path=str(dest),
                                old_value=str(file_path),
                                stage=self.NAME,
                                note=reason,
                            )
                        except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
                            # file_path is UNIQUE and IntegrityError is not an
                            # OSError, so the handler below never caught it:
                            # the file was already in QUARANTINE and the
                            # exception escaped, aborting the stage with disk
                            # and database disagreeing and no manifest at all
                            # -- CorruptStage writes none. (P0-D, 2026-09-09.)
                            try:
                                shutil.move(str(dest), str(file_path))
                                note = "move reverted"
                            except OSError as revert_exc:
                                note = (
                                    f"COULD NOT REVERT -- the file is at {dest} "
                                    f"while its row still points at {file_path}; "
                                    f"needs a manual fix ({revert_exc})"
                                )
                            logger.error(
                                "[%s] DB collision quarantining %s (%s); %s",
                                self.NAME,
                                file_path,
                                exc,
                                note,
                            )
                            result.files_errored += 1
                            result.errors.append(f"{file_path.name}: DB collision, {note}")
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
                result.notes.append(f"  {elision(len(suspects) - 10)}")
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

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """A file this stage says it quarantined must actually be in quarantine.

        Corrupt moves files out of the library and rewrites status and
        file_path in one step. If the move fails but the row is written
        anyway, the DB points at a path holding nothing and the corrupt
        file stays in the library -- the row says handled, the disk says
        otherwise, and every later stage skips it as QUARANTINED.

        Checks the DISK, not the status column: that a QUARANTINED row's
        file is where the row now claims, and that the claim is inside the
        quarantine tree rather than still in the library.
        """
        rows = ctx.conn.execute(
            """
            SELECT a.file_path FROM archive a
              JOIN events e ON e.file_path = a.file_path
             WHERE a.status = 'QUARANTINED' AND e.run_id = ?
               AND e.event_type IN ('QUARANTINE', 'CORRUPT_DETECTED')
             ORDER BY e.id DESC LIMIT 8
            """,
            (ctx.run_id,),
        ).fetchall()
        if not rows:
            return []
        problems: list[str] = []
        qroot = str(ctx.quarantine)
        for r in rows:
            p = Path(r["file_path"])
            if not p.exists():
                problems.append(f"quarantined row points at nothing: {p.name}")
            elif not str(p).startswith(qroot):
                problems.append(f"{p.name}: marked QUARANTINED but still outside {qroot}")
        return problems

    #: Statuses a row holds after Ingest but before Scholar catalogues it.
    #: An arrival Scholar HAS catalogued but Finalize has not yet filed is an
    #: arrival too; _gate_new_arrivals asks finalized_at for those.
    ARRIVAL_STATUSES = ("PENDING", "HASHED")

    def _gate_new_arrivals(self, ctx: RunContext, result: StageResult, dry_run: bool) -> None:
        """Decode every file arriving this run, before Finalize can catalogue it.

        Why this exists
        ---------------
        `ffmpeg_decode_check` was called from exactly one place: the
        `_scan()` sweep, which decodes only a budget of never-checked rows a
        run. CorruptStage runs BEFORE Finalize -- so on the run that ingests
        a file, nothing was sure to decode it: only the size-ratio heuristic
        or one of NEW_ARRIVAL_DECODE_BUDGET slots would.

        That is how five truncated files reached the library as masters on
        2026-09-21 (Billy Joel, Billy Ocean, Chic, The Who, Tower of Power).
        All five were right-sized, so the shape heuristic could not see them,
        and metadata cannot see truncation at all: in MP4 both durations live
        in the `moov` atom, written before the audio, so a file cut short
        still reports its full length. Only a decode knows.

        No budget here, deliberately. `_scan()` is bounded because it re-reads
        a library that only grows; this gate reads one batch, so its cost is
        proportional to what arrived (~1.7 files/sec measured over 9,414 files
        on 2026-09-21) rather than to the library size. A damaged master that
        reaches ALAC-Archival propagates into every tier built from it, and no
        later stage can tell it was ever wrong.
        """
        conn = ctx.conn
        placeholders = ",".join("?" for _ in self.ARRIVAL_STATUSES)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(archive)").fetchall()}
        # Scholar runs BEFORE this stage and catalogues every arrival it
        # reads, so by now an arrival is normally CATALOGUED, not HASHED. The
        # gate asked for PENDING/HASHED only and so saw almost nothing: on
        # 2026-10-11 8,118 of the 8,819 masters had never been decoded, and
        # the run that filed them had decoded only _scan's budget of 200
        # (found tracing Grey's code review, 2026-10-11, finding 2). Not yet
        # filed by Finalize is what makes a row an arrival.
        not_filed = (
            " OR (status = 'CATALOGUED' AND (finalized_at IS NULL OR finalized_at = ''))"
            if "finalized_at" in cols
            else ""
        )
        # One already decoded clean -- by _scan's budget above, or a run cut
        # short -- is not decoded twice.
        unchecked = " AND (decode_ok IS NULL OR decode_ok != 1)" if "decode_ok" in cols else ""
        rows = conn.execute(
            f"SELECT file_path, artist, title FROM archive "
            f"WHERE (status IN ({placeholders}){not_filed}) AND file_path IS NOT NULL"
            f"{unchecked}",
            self.ARRIVAL_STATUSES,
        ).fetchall()
        if not rows:
            return
        logger.info("[%s] decode-gating %d new arrival(s)", self.NAME, len(rows))

        # The configured quarantine (MUSAEUS_QUARANTINE), which verify_effect
        # checks against -- not vault_root/QUARANTINE, which it only is by
        # default (Grey's code review, 2026-10-11, finding 9).
        quarantine_dir = ctx.quarantine / "corrupted"
        gated = 0
        lib = ctx.alac_library
        for row in rows:
            file_path = Path(row["file_path"])
            if not file_path.is_file():
                continue
            # Never a file in ALAC_Library: it is an edition, not an arrival,
            # and quarantining it would take it straight out of the library
            # (Finalize skips them the same way; pre-merge review of #179).
            if lib is not None and file_path.is_relative_to(lib):
                continue
            try:
                decoded_ok, decode_err = ffmpeg_decode_check(file_path, seconds=0)
            except DecodeUnavailable as exc:
                # Not checked (review of slice B, finding 1): nothing recorded
                # or moved. The stage did not do its job for this arrival, so
                # it fails rather than reporting success with an error line
                # (Grey's code review, 2026-10-11, finding 2). Finalize holds
                # the arrival back until a later run decodes it.
                result.files_errored += 1
                result.errors.append(f"{file_path.name}: arrival not decoded: {exc}")
                result.success = False
                continue
            if not dry_run:
                conn.execute(
                    "UPDATE archive SET decode_checked_at = datetime('now'), "
                    "decode_ok = ?, decode_errors = ? WHERE file_path = ?",
                    (1 if decoded_ok else 0, 0 if decoded_ok else 1, str(file_path)),
                )
            if decoded_ok:
                continue

            logger.warning(
                "[%s] arrival does NOT decode, refusing to catalogue: %s — %s",
                self.NAME,
                file_path.name,
                (decode_err or "")[:120],
            )
            gated += 1
            result.files_errored += 1
            if dry_run:
                continue

            quarantine_dir.mkdir(parents=True, exist_ok=True)
            dest = _quarantine_dest(conn, quarantine_dir, file_path)
            import shutil

            try:
                shutil.move(str(file_path), str(dest))
            except OSError as exc:
                logger.error("[%s] could not quarantine %s: %s", self.NAME, file_path.name, exc)
                result.errors.append(f"{file_path.name}: not quarantined: {exc}")
                continue
            try:
                conn.execute(
                    "UPDATE archive SET status='QUARANTINED', file_path=? WHERE file_path=?",
                    (str(dest), str(file_path)),
                )
            except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
                # The file moved but its row could not follow: put the file
                # back, so disk and database agree (review of slice B,
                # finding 5 -- this logged and carried on, leaving the row at
                # an INBOX path holding nothing).
                try:
                    shutil.move(str(dest), str(file_path))
                    note = "move reverted"
                except OSError as revert_exc:
                    note = (
                        f"COULD NOT REVERT -- the file is at {dest} while its row "
                        f"still points at {file_path}; needs a manual fix ({revert_exc})"
                    )
                logger.error("[%s] DB collision quarantining %s (%s); %s",
                             self.NAME, file_path, exc, note)  # fmt: skip
                result.errors.append(f"{file_path.name}: DB collision, {note}")
                continue
            ctx.log_event(
                "QUARANTINE",
                file_path=str(dest),
                old_value=str(file_path),
                stage=self.NAME,
                note=f"arrival does not decode: {(decode_err or '')[:120]}",
            )
            logger.info("[%s] → quarantined arrival: %s", self.NAME, dest.name)
            result.files_changed += 1  # verify_effect checks it
        if gated:
            logger.warning(
                "[%s] %d arrival(s) refused: damaged before they reached the library",
                self.NAME,
                gated,
            )

    def run(self, ctx: RunContext) -> StageResult:
        result = self._scan(ctx, dry_run=False)
        self._gate_new_arrivals(ctx, result, dry_run=False)
        return result
