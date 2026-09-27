"""The loudness bake: one master in, one -18 LUFS lossless copy out.

Moved here from scripts/alac_library/build_alac_library.py (retired
2026-09-25 because it REPOINTED catalogue rows at the copies it made). The
mechanics are unchanged and execution-proven there: a two-pass EBU R128
loudnorm in linear mode, the master's own sample format kept (a 16-bit master
baked unpinned came out 24-bit -- 61% bigger, all padding), cover art copied,
and every bake verified before it is trusted.

Two additions, both from the 2026-09-26 design review:

  sample rate   loudnorm works at 192 kHz internally; the output rate is
                pinned to the master's, so a 44.1 kHz master never becomes a
                192 kHz copy.
  dynamic mode  linear loudnorm falls back to DYNAMIC (compressing the music)
                when the target cannot be reached by gain alone -- quiet
                classical that needs lifting is the usual case. The mode is
                read from the second pass and reported, never hidden.

Nothing here touches the database. edition_build.py decides what to bake and
records what was made.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .duration import tolerance_for
from .editions import LOSSLESS

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
#: The edition's one definition of its target lives in editions.LOSSLESS.
TARGET_I = f"{LOSSLESS.lufs_target:.1f}"
TARGET_TP = "-1.0"
#: The loudness-range target, at ffmpeg's maximum. loudnorm leaves linear
#: mode -- one fixed gain -- for DYNAMIC (compression) when a track's range
#: exceeds this, as well as when the gain would push its peaks past TP.
#: At 11 it compressed tracks that only needed turning down: Barenaked
#: Ladies' "Aluminum" (range 14.2) went dynamic for a 1.3 dB cut. Grey,
#: 2026-09-27: compress only when the peaks force it.
TARGET_LRA = "50.0"

_LUFS_TOLERANCE = 1.0
# Deadlines count WORKING time (see _run): the idle throttle SIGSTOPs these
# children while the machine is in use, and paused time is not stalled work.
_PROBE_TIMEOUT = 60
_BAKE_TIMEOUT = 1800
_DECODE_TIMEOUT = 900
_DEADLINE_POLL_S = 1.0

#: The IdleThrottle holding this module's children, set by the edition build
#: for as long as it runs; its paused_seconds is taken off every deadline.
ACTIVE_THROTTLE: Any = None

_OUTPUT_I_RE = re.compile(r"Output Integrated:\s*(-?\d+(?:\.\d+)?)\s*LUFS", re.I)
_NORM_TYPE_RE = re.compile(r"Normalization Type:\s*(\w+)", re.I)


class BakeError(RuntimeError):
    """The bake did not produce a copy that can be trusted."""


@dataclass(frozen=True)
class BakeResult:
    achieved_lufs: float | None
    mode: str  # "linear" | "dynamic" | "" when loudnorm did not say


def _paused() -> float:
    return float(ACTIVE_THROTTLE.paused_seconds) if ACTIVE_THROTTLE is not None else 0.0


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Run *cmd* with a deadline on WORKING time, stdin closed.

    The retired script's _run_with_deadline, restored (cloud review of #49):
    a flat wall-clock timeout killed work the idle throttle had merely
    paused -- measured 2026-09-01, a 0.6 s bake sat past 90 s while someone
    used the machine. And stdin is closed: two workers' ffmpegs otherwise
    share the terminal, read keystrokes as commands ('q' stops an encode)
    and leave the tty without echo (CLAUDE.md: every ffmpeg in a loop needs
    -nostdin).
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    start, paused_at_start = time.monotonic(), _paused()
    while True:
        try:
            out, err = proc.communicate(timeout=_DEADLINE_POLL_S)
        except subprocess.TimeoutExpired:
            working = (time.monotonic() - start) - (_paused() - paused_at_start)
            if working <= timeout:
                continue
            proc.kill()
            proc.communicate()
            raise BakeError(f"{cmd[0]} made no progress in {timeout}s of working time") from None
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def probe(path: Path) -> dict:
    proc = _run(
        [
            FFPROBE,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        _PROBE_TIMEOUT,
    )
    if proc.returncode != 0:
        raise BakeError(f"ffprobe failed on {path} ({proc.returncode}): {proc.stderr[:200]}")
    data: dict = json.loads(proc.stdout)
    return data


def _audio_stream(info: dict) -> dict:
    for s in info.get("streams", []):
        if s.get("codec_type") == "audio":
            return dict(s)
    return {}


def has_attached_picture(info: dict) -> bool:
    return any(
        s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") == 1
        for s in info.get("streams", [])
    )


def sample_fmt_for(info: dict) -> str | None:
    """The master's own depth: s16p for 16-bit, s32p for more, None if unknown."""
    a = _audio_stream(info)
    depth = a.get("bits_per_raw_sample") or a.get("bits_per_sample")
    try:
        bits = int(str(depth))
    except (TypeError, ValueError):
        return None
    if bits <= 0:
        return None
    return "s16p" if bits <= 16 else "s32p"


def sample_rate_of(info: dict) -> int | None:
    try:
        return int(str(_audio_stream(info).get("sample_rate")))
    except (TypeError, ValueError):
        return None


def ffmpeg_measure_loudnorm(path: Path) -> dict:
    """First pass: what loudnorm measures of the master."""
    flt = f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}:print_format=json"
    proc = _run(
        [
            FFMPEG,
            "-nostdin",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-af",
            flt,
            "-f",
            "null",
            "-",
        ],
        _BAKE_TIMEOUT,
    )
    err = proc.stderr or ""
    start, end = err.rfind("{"), err.rfind("}")
    if start == -1 or end <= start:
        raise BakeError(f"could not read the loudness measurement of {path.name}")
    measured: dict = json.loads(err[start : end + 1])
    return measured


def build_second_pass_filter(measured: dict) -> str:
    """The bake: the first pass's measurements applied in linear mode. The
    repo's name for it, so tests/test_bake_is_always_two_pass.py guards it
    (which is why this docstring names no filter option: the guard reads
    this function's text)."""
    return (
        f"loudnorm=measured_I={measured['input_i']}:measured_LRA={measured['input_lra']}:"
        f"measured_TP={measured['input_tp']}:measured_thresh={measured['input_thresh']}:"
        f"offset={measured['target_offset']}:I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}:"
        "linear=true:print_format=summary"
    )


def bake_command(source: Path, output: Path, loudnorm: str, info: dict) -> list[str]:
    """ALAC -> ALAC at the master's own rate and depth, art and tags copied."""
    fmt = sample_fmt_for(info)
    rate = sample_rate_of(info)
    audio = [
        "-c:a",
        "alac",
        *(["-sample_fmt", fmt] if fmt else []),
        *(["-ar", str(rate)] if rate else []),
        "-af",
        loudnorm,
    ]
    cmd = [
        FFMPEG,
        "-nostdin",
        "-hide_banner",
        "-nostats",
        "-y",
        "-i",
        str(source),
        "-threads",
        "2",
        "-map",
        "0:a:0",
    ]
    if has_attached_picture(info):
        cmd += ["-map", "0:v:0", *audio, "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    else:
        cmd += audio
    return [*cmd, "-map_metadata", "0", "-f", "mp4", str(output)]


def parse_achieved(stderr: str) -> float | None:
    m = _OUTPUT_I_RE.search(stderr or "")
    return float(m.group(1)) if m else None


def parse_mode(stderr: str) -> str:
    m = _NORM_TYPE_RE.search(stderr or "")
    return m.group(1).lower() if m else ""


def verify(source_info: dict, output: Path, achieved: float | None) -> None:
    """The copy has audio, the master's rate, its length, and the target loudness."""
    out = probe(output)
    a = _audio_stream(out)
    if not a:
        raise BakeError("the copy has no audio stream")
    want_rate = sample_rate_of(source_info)
    if want_rate and sample_rate_of(out) != want_rate:
        raise BakeError(f"the copy is {sample_rate_of(out)} Hz, the master {want_rate} Hz")
    if achieved is None:
        raise BakeError("loudnorm did not report the loudness it achieved -- unverified")
    if abs(achieved - float(TARGET_I)) > _LUFS_TOLERANCE:
        raise BakeError(f"baked to {achieved:.2f} LUFS, wanted {TARGET_I}")

    def _dur(info: dict) -> float | None:
        try:
            return float(info.get("format", {}).get("duration"))
        except (TypeError, ValueError):
            return None

    sd, od = _dur(source_info), _dur(out)
    if sd is not None and od is not None and abs(sd - od) > tolerance_for(sd):
        raise BakeError(f"length changed: master {sd:.1f}s, copy {od:.1f}s")


#: Freeform tags that carry a loudness GAIN. A copy already baked to -18 LUFS
#: must not carry its master's: a player would apply the gain a second time.
_GAIN_TAG_RE = re.compile(r"^----:com\.apple\.iTunes:(r128_|replaygain_|itunnorm)", re.I)

#: Written on every edition copy, so a copy is recognisable as one -- and as
#: the copy of WHICH master -- from the file alone, even when the record of
#: it was lost to an interruption between the rename and the write.
MARKER_KEY = "----:com.apple.iTunes:MUSAEUS_EDITION"


def copy_tags(master: Path, copy: Path, marker: str) -> None:
    """Give *copy* the master's tags, minus any loudness gain, plus *marker*.

    Measured 2026-09-27 on a real master: ffmpeg's -map_metadata 0 keeps
    the plain iTunes atoms but drops every freeform one -- the gain tags,
    which is right, and also BPM, key, ISRC, the MusicBrainz id and the
    track counts, which is not. So the tags are copied here instead.
    """
    from mutagen.mp4 import MP4, MP4FreeForm

    src = MP4(master)
    dst = MP4(copy)
    if dst.tags is None:
        dst.add_tags()
    assert dst.tags is not None
    dst.tags.clear()
    for key, value in (src.tags or {}).items():
        if not _GAIN_TAG_RE.match(key):
            dst.tags[key] = value
    dst.tags[MARKER_KEY] = [MP4FreeForm(marker.encode("utf-8"))]
    dst.save()


def read_marker(path: Path) -> str | None:
    """The edition marker on *path*, or None when it carries none."""
    from mutagen.mp4 import MP4

    try:
        tags: Any = MP4(path).tags or {}
    except Exception:  # noqa: BLE001 -- unreadable means "not ours"
        return None
    values = tags.get(MARKER_KEY)
    if not values:
        return None
    return bytes(values[0]).decode("utf-8", "replace")


def bake(source: Path, tmp_output: Path) -> BakeResult:
    """Bake *source* into *tmp_output* and verify it. Raises BakeError.

    The caller moves tmp_output into place only after this returns: a copy
    that fails verification never sits where a finished one would.
    """
    info = probe(source)
    measured = ffmpeg_measure_loudnorm(source)
    proc = _run(
        bake_command(source, tmp_output, build_second_pass_filter(measured), info), _BAKE_TIMEOUT
    )
    if proc.returncode != 0:
        raise BakeError(f"ffmpeg exited {proc.returncode}: {(proc.stderr or '')[-200:]}")
    result = BakeResult(parse_achieved(proc.stderr), parse_mode(proc.stderr))
    verify(info, tmp_output, result.achieved_lufs)
    return result


def codec_of(path: Path) -> str:
    """The audio codec ffprobe reports, for a row with none recorded."""
    try:
        return str(_audio_stream(probe(path)).get("codec_name") or "")
    except BakeError:
        return ""


def decode_problem(path: Path) -> str | None:
    """None when the master decodes cleanly, else the first audio error.

    The retired script's decode gate, restored (cloud review of #49): a
    master damaged inside its stream bakes "successfully" -- ffmpeg exits 0
    -- and the copy then decodes clean, hiding the damage from any later
    audit of the edition. The command and the judgement of which stderr is
    audio damage are musaeus.duration.decodes_cleanly's; only the running
    differs (working-time deadline, stdin closed).
    """
    from .stages.corrupt import audio_relevant_stderr, audio_stream_index

    proc = _run(
        [FFMPEG, "-nostdin", "-v", "error", "-nostats", "-i", str(path), "-vn", "-f", "null", "-"],
        _DECODE_TIMEOUT,
    )
    stderr = (proc.stderr or "").strip()
    if stderr:
        stderr = audio_relevant_stderr(stderr, audio_stream_index(path))
    if proc.returncode != 0 or stderr:
        return stderr.splitlines()[0] if stderr else f"ffmpeg exited {proc.returncode}"
    return None
