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

import contextlib
import json
import math
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .duration import REENCODE_TOLERANCE_SEC, tolerance_for
from .editions import CAR, LOSSLESS

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
    #: What was measured and how (edition_ledger keeps it: Grey, 2026-09-28,
    #: a song is measured once). Empty when a stand-in bake did not say.
    recipe: str = ""
    measured: dict | None = None


#: The fields build_second_pass_filter reads. A kept measurement missing any
#: of them is not used -- the song is measured again.
_MEASURED_FIELDS = ("input_i", "input_lra", "input_tp", "input_thresh", "target_offset")


def _measure_or_reuse(
    recipe: str, known: Mapping[str, dict] | None, measure: Callable[[], dict]
) -> dict:
    kept = (known or {}).get(recipe)
    if kept is not None and all(k in kept for k in _MEASURED_FIELDS):
        return kept
    return measure()


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
    with _CHILDREN_LOCK:
        _CHILDREN.add(proc)
    try:
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
                raise BakeError(
                    f"{cmd[0]} made no progress in {timeout}s of working time"
                ) from None
            return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    finally:
        with _CHILDREN_LOCK:
            _CHILDREN.discard(proc)


#: Every child _run has running, so a stopped build can end them: stopped by
#: SIGTERM, an ffmpeg kept encoding after the build had let go of its lock
#: (cloud review of #53).
_CHILDREN: set[subprocess.Popen] = set()
_CHILDREN_LOCK = threading.Lock()


def stop_children() -> None:
    """Kill every child _run has running. For a build being stopped."""
    with _CHILDREN_LOCK:
        running = list(_CHILDREN)
    for proc in running:
        with contextlib.suppress(OSError):
            proc.kill()


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


def ffmpeg_measure_loudnorm(
    path: Path,
    target_i: str = TARGET_I,
    target_tp: str = TARGET_TP,
    target_lra: str = TARGET_LRA,
    before: str = "",
    timeout: int | None = None,
) -> dict:
    """First pass: what loudnorm measures of the master (after *before*,
    the same filters the second pass runs ahead of loudnorm)."""
    flt = f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json"
    if before:
        flt = f"{before},{flt}"
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
        timeout or _BAKE_TIMEOUT,
    )
    err = proc.stderr or ""
    if proc.returncode != 0:
        # loudnorm prints its JSON even when ffmpeg stops early: a partial
        # measurement must not steer the second pass (second review of #49).
        raise BakeError(f"measuring {path.name}: ffmpeg exited {proc.returncode}: {err[-200:]}")
    start, end = err.rfind("{"), err.rfind("}")
    if start == -1 or end <= start:
        raise BakeError(f"could not read the loudness measurement of {path.name}")
    measured: dict = json.loads(err[start : end + 1])
    # Kept and reused, so it must be numbers: loudnorm prints "-inf" for
    # silence, and a partial run can leave a field out.
    for key in _MEASURED_FIELDS:
        try:
            value = float(measured[key])
        except (KeyError, TypeError, ValueError):
            raise BakeError(f"the loudness measurement of {path.name} has no {key}") from None
        if not math.isfinite(value):
            raise BakeError(f"the loudness measurement of {path.name} gave {key} = {value}")
    return measured


def build_second_pass_filter(
    measured: dict,
    target_i: str = TARGET_I,
    target_tp: str = TARGET_TP,
    target_lra: str = TARGET_LRA,
) -> str:
    """The bake: the first pass's measurements applied in linear mode. The
    repo's name for it, so tests/test_bake_is_always_two_pass.py guards it
    (which is why this docstring names no filter option: the guard reads
    this function's text)."""
    return (
        f"loudnorm=measured_I={measured['input_i']}:measured_LRA={measured['input_lra']}:"
        f"measured_TP={measured['input_tp']}:measured_thresh={measured['input_thresh']}:"
        f"offset={measured['target_offset']}:I={target_i}:TP={target_tp}:LRA={target_lra}:"
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


#: How far BELOW the target a DYNAMIC-mode copy may land and still be kept.
#: Linear mode lands on the target; dynamic mode, on a quiet master with big
#: peaks, can fall short -- Handel's "Zadok The Priest" reaches -19.5 at
#: best and was refused on every build. Grey, 2026-09-28: include such a
#: track at its best level rather than leave it out. Never louder than the
#: target, and never a linear copy off it.
_DYNAMIC_SHORTFALL = 2.0


def verify(
    source_info: dict,
    output: Path,
    achieved: float | None,
    mode: str = "linear",
    *,
    target_i: str = TARGET_I,
    rate: int | None = None,
    codec: str | None = None,
    max_channels: int | None = None,
    length_tolerance: float | None = None,
) -> None:
    """The copy has audio, the right rate, its length, and the target loudness.

    *length_tolerance*, when given, replaces the shared drift rule (2 s or
    2 %) for the length.

    *rate* is the rate the copy must have -- the master's own unless given
    (the AAC editions cap it). *codec* and *max_channels*, when given, are
    checked too: measure the artifact, not the report.
    """
    out = probe(output)
    a = _audio_stream(out)
    if not a:
        raise BakeError("the copy has no audio stream")
    want_rate = rate or sample_rate_of(source_info)
    if want_rate and sample_rate_of(out) != want_rate:
        raise BakeError(f"the copy is {sample_rate_of(out)} Hz, wanted {want_rate} Hz")
    if codec and a.get("codec_name") != codec:
        raise BakeError(f"the copy is {a.get('codec_name')}, wanted {codec}")
    if max_channels and int(a.get("channels") or 0) > max_channels:
        raise BakeError(f"the copy has {a.get('channels')} channels, at most {max_channels}")
    if achieved is None:
        raise BakeError("loudnorm did not report the loudness it achieved -- unverified")
    off = achieved - float(target_i)
    short_but_best = mode == "dynamic" and -_DYNAMIC_SHORTFALL <= off < 0
    if abs(off) > _LUFS_TOLERANCE and not short_but_best:
        raise BakeError(f"baked to {achieved:.2f} LUFS, wanted {target_i}")

    def _dur(info: dict) -> float | None:
        try:
            return float(info.get("format", {}).get("duration"))
        except (TypeError, ValueError):
            return None

    sd, od = _dur(source_info), _dur(out)
    allowed = length_tolerance if length_tolerance is not None else tolerance_for(sd)
    if sd is not None and od is not None and abs(sd - od) > allowed:
        raise BakeError(f"length changed: master {sd:.2f}s, copy {od:.2f}s")


#: Freeform tags that carry a loudness GAIN. A copy already baked to -18 LUFS
#: must not carry its master's: a player would apply the gain a second time.
#: Not carried to a copy: the master's loudness gain (the copy is baked to
#: its target), and its encoder's own notes -- iTunSMPB (priming, padding,
#: length) and Encoding Params describe the master's encode, and a player
#: honouring the master's iTunSMPB cut 48 ms off the start of a new AAC copy
#: (cloud review of #53).
_GAIN_TAG_RE = re.compile(
    r"^----:com\.apple\.iTunes:(r128_|replaygain_|itunnorm|itunsmpb$|encoding params$)", re.I
)

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


#: The Lossless bake's measurement: loudnorm's targets, no filters before it.
LOSSLESS_RECIPE = f"alac I={TARGET_I} TP={TARGET_TP} LRA={TARGET_LRA}"


def bake(source: Path, tmp_output: Path, known: Mapping[str, dict] | None = None) -> BakeResult:
    """Bake *source* into *tmp_output* and verify it. Raises BakeError.

    The caller moves tmp_output into place only after this returns: a copy
    that fails verification never sits where a finished one would. *known*
    is the record's kept measurements of this audio, by recipe.
    """
    info = probe(source)
    measured = _measure_or_reuse(LOSSLESS_RECIPE, known, lambda: ffmpeg_measure_loudnorm(source))
    proc = _run(
        bake_command(source, tmp_output, build_second_pass_filter(measured), info), _BAKE_TIMEOUT
    )
    if proc.returncode != 0:
        raise BakeError(f"ffmpeg exited {proc.returncode}: {(proc.stderr or '')[-200:]}")
    result = BakeResult(
        parse_achieved(proc.stderr), parse_mode(proc.stderr), LOSSLESS_RECIPE, measured
    )
    verify(info, tmp_output, result.achieved_lufs, result.mode)
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


# ── The AAC editions: car and iPhone ────────────────────────────────────────
#
# One ffmpeg pass per track, from the master: measure, apply the measured
# loudness (the music lands on -14), cap the rate, fold anything wider than
# stereo to stereo, mix the noise under it (car only), limit, encode AAC.
# The old car builder encoded AAC, decoded it, mixed the noise and encoded
# AAC again -- every car track lossy twice.

AAC_TARGET_I = f"{CAR.lufs_target:.1f}"
#: ffmpeg's normal range target. Grey, 2026-09-28: in the car, compression
#: that keeps quiet passages audible over road noise is welcome.
AAC_TARGET_LRA = "11.0"
AAC_BITRATE = f"{CAR.bitrate_kbps}k"

#: Grey's levels for the noise under every car track (the old masker's).
NOISE_LEVELS_DB = {"brown": -12.0, "pink": -15.0, "white": -18.0}
#: Those levels applied to noise at -16 LUFS: the old noise beds were each
#: colour brought to -16 LUFS. anoisesrc at amplitude 1, measured 2026-09-28
#: (60 s, 44.1 and 48 kHz within 0.1): brown -15.6, pink -14.7, white -1.7.
NOISE_BED_LUFS = -16.0
NOISE_RAW_LUFS = {"brown": -15.6, "pink": -14.7, "white": -1.7}
#: Fixed seeds: the same master gives the same car file on every build.
NOISE_SEEDS = {"brown": 1, "pink": 2, "white": 3}
#: The old masker's ceiling: -0.2 dBFS. amix with normalize=0 adds the noise
#: on top of the music, so a limiter is not optional -- and not for the
#: iPhone either: loudnorm limits at 192 kHz, and the way back down to the
#: copy's rate plus the AAC encode put Spirit Of The West's iPhone copy at
#: +1.9 dBTP (rehearsal, 2026-09-28; its car copy, limited, was -0.4). A
#: lower ceiling did not help the one car over left (Beck, 5.1: +0.1 at this
#: ceiling, +0.2 at -1 dBFS) -- that one is the AAC encoder's own.
CEILING = 0.977


def _deadline(seconds: float) -> int:
    """Working seconds an ffmpeg call on a song this long may take: the flat
    limit, or one second per second of audio if that is more. The encode
    runs at about a tenth of that; a multi-hour master must not be killed
    as hung on every build (cloud review of #53)."""
    return max(_BAKE_TIMEOUT, int(seconds) + 1)


#: How far the finished AAC file may measure from the loudness loudnorm
#: reported for the music. The noise adds a little: rehearsal, 2026-09-28,
#: +0.0 to +0.4 LU in the car, within 0.1 on the iPhone.
_FINISHED_TOLERANCE = 1.0


def loudness_of(path: Path, timeout: int | None = None) -> tuple[float, float, float]:
    """(integrated LUFS, quietest moment LUFS, true peak dBTP), measured
    from the file itself.

    loudnorm's own figures are the music before the noise, the limiter and
    the encode (CLAUDE.md: measure the artifact, not the report). The
    quietest moment is the lowest 400 ms reading after the first half second
    (the first readings are of a window not yet full). The true peak is the
    encoded file's: the AAC encoder itself overshoots the limiter.
    """
    proc = _run(
        [FFMPEG, "-nostdin", "-hide_banner", "-nostats", "-i", str(path),
         "-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", "-"],
        timeout or _BAKE_TIMEOUT,
    )  # fmt: skip
    if proc.returncode != 0:
        raise BakeError(f"measuring {path.name}: ffmpeg exited {proc.returncode}")
    err = proc.stderr or ""
    found = re.findall(r"I:\s+(-?\d+(?:\.\d+)?) LUFS", err)
    moments = [
        float(m)
        for t, m in re.findall(r"t:\s*([\d.]+)\s+TARGET:.*?M:\s*(-?[\d.]+)", err)
        if float(t) >= 0.5
    ]
    peaks = re.findall(r"Peak:\s+(-?(?:\d+(?:\.\d+)?|inf)) dBFS", err)
    if not found or not moments or not peaks:
        raise BakeError(f"could not measure the loudness of {path.name}")
    return float(found[-1]), min(moments), float(peaks[-1])  # the summary is the last


#: Bumped when the AAC graph changes what a copy sounds like, so the copies
#: made the old way are made again (2: frames steadied for ffmpeg 6.1,
#: 2026-09-29).
# 3: a copy peaking over is encoded again. 4: no noise substitution (both
# 2026-09-29).
AAC_GRAPH_VERSION = 4

#: The AAC encoder's perceptual noise substitution (ffmpeg's default) is off.
#: It replaces noise-like bands with synthesised noise -- and every car song
#: has noise under it -- and its spikes defeated the limiter: Billie Jean
#: peaked +3.0 dBTP with it, -0.6 without (-1.2 before the encoder); the
#: peak jumped about as the limiter was lowered, so retries never caught it.
#: A low-bitrate tool; at 256k it saves nothing worth having.

#: No AAC copy may peak over this, measured after the encode. When one does,
#: it is encoded again with the limiter lowered by the overshoot, aiming for
#: _PEAK_AIM_DBTP. The 200-song vault build, 2026-09-29: 11 of 200 car copies
#: peaked over 0 dBTP, one at +2.1 -- -0.2 before the encoder (Grey: fix it).
_PEAK_LIMIT_DBTP = 0.0
_PEAK_AIM_DBTP = -0.5
_PEAK_RETRIES = 2
#: Still this far over after the tries: refused, not kept.
_PEAK_REFUSE_DBTP = 0.5


def aac_settings(noise: bool) -> str:
    """What an AAC copy is made with, as the record keeps it: a copy made
    with anything else is made again (Grey, 2026-09-29)."""
    made = (
        f"aac graph={AAC_GRAPH_VERSION} {AAC_BITRATE} pns=off I={AAC_TARGET_I} TP={TARGET_TP} "
        f"LRA={AAC_TARGET_LRA} ceiling={CEILING}"
    )
    if not noise:
        return made
    beds = " ".join(
        f"{c}={NOISE_LEVELS_DB[c]:+g}/{NOISE_SEEDS[c]}" for c in sorted(NOISE_LEVELS_DB)
    )
    return f"{made} noise {beds} bed={NOISE_BED_LUFS:g}"


def noise_bed_lufs() -> float:
    """The loudness of the three noise beds together, under every car song."""
    return 10 * math.log10(
        sum(10 ** ((NOISE_BED_LUFS + NOISE_LEVELS_DB[c]) / 10) for c in NOISE_LEVELS_DB)
    )


#: How far under the noise bed the quietest moment of a car copy may read
#: before the noise is missing. Measured 2026-09-29: the vault's car copies'
#: quietest moments were -26.1 to -25.6 (the bed, -25.6); the same songs'
#: iPhone copies, no noise, -42.9 and far below.
_NOISE_MARGIN = 4.0


def noise_gain_db(colour: str) -> float:
    """The gain that brings *colour* from anoisesrc to its level under the music."""
    return NOISE_BED_LUFS - NOISE_RAW_LUFS[colour] + NOISE_LEVELS_DB[colour]


def target_rate(source_rate: int | None) -> int | None:
    """The AAC editions' rate: the master's own up to 48 kHz, else 48 or 44.1.

    Ported from the old car encoder (build_aac_library._target_rate), where
    it was measured: the rate must always be stated, because loudnorm
    resamples internally and an unpinned encode takes the filter's rate (a
    44.1 kHz master came out 96 kHz, 2026-08-31). Above 48 kHz each rate
    stays in its own family, so the ratio is exact (192->48, 88.2->44.1).
    """
    if not source_rate:
        return None
    if source_rate <= 48_000:
        # The next rate the AAC encoder takes: a master at 37.8, 44.056 or
        # 47.25 kHz failed the encode on every build (cloud review of #53).
        return next(r for r in _AAC_RATES if r >= source_rate)
    return 44_100 if source_rate % 44_100 == 0 else 48_000


#: The rates ffmpeg's AAC encoder takes, up to the editions' 48 kHz cap.
_AAC_RATES = (7_350, 8_000, 11_025, 12_000, 16_000, 22_050, 24_000, 32_000, 44_100, 48_000)


def channels_of(info: dict) -> int:
    try:
        return int(str(_audio_stream(info).get("channels")))
    except (TypeError, ValueError):
        return 0


def _layout(channels: int) -> str:
    return "mono" if channels == 1 else "stereo"


def aac_before_loudnorm(rate: int, channels: int) -> str:
    """The rate and the fold to stereo, ahead of BOTH loudnorm passes.

    Folded after loudnorm, a 5.1 master was measured as six channels and
    then summed: a synthetic one reported -13.5 and played at -10.5
    (2026-09-28). Measured here, the fold is what loudnorm sees, and the
    192 kHz 5.1 Beck track took 48 s instead of 178.
    """
    # rematrix_maxval=1: the fold never clips, whatever sample format ffmpeg
    # negotiates. Unset, an integer fold was scaled down and a float one was
    # not -- the two passes measured the same master 8 LU apart.
    fold = ":ochl=stereo:rematrix_maxval=1.0" if channels > 2 else ""  # mono stays mono
    return f"aresample={rate}{fold},aformat=channel_layouts={_layout(channels)}"


#: Every AAC recipe starts with this: the car and the iPhone share it.
AAC_RECIPE_FAMILY = f"aac I={AAC_TARGET_I} TP={TARGET_TP} LRA={AAC_TARGET_LRA}"


def aac_recipe(rate: int, channels: int) -> str:
    """The AAC measurement's recipe: its targets and the filters before it."""
    return f"{AAC_RECIPE_FAMILY} | {aac_before_loudnorm(rate, channels)}"


def aac_filter(
    loudnorm: str,
    rate: int,
    channels: int,
    seconds: float,
    noise: bool,
    ceiling: float | None = None,
) -> str:
    """The one filter graph: music at the target, noise under it, limited."""
    # The layout is STATED on both sides of the mix. Left to negotiation,
    # loudnorm plus the three-colour mix collapsed a stereo master to mono
    # (measured 2026-09-28; each part alone stayed stereo) -- CLAUDE.md: an
    # unstated format property is decided by the input. The rate is stated
    # again after loudnorm, which always outputs 192 kHz.
    layout = _layout(channels)
    # asetnsamples: steady 1024-sample frames, the last one not padded. On
    # ffmpeg 6.1 amix's duration=first ended the car mix 0.2-0.7 s before
    # the music without it (CI, cloud review of #53); 5.1 was exact either
    # way. Mixing to the noise's end instead would pad a truncated master
    # with noise and pass it.
    music = (
        f"[0:a:0]{aac_before_loudnorm(rate, channels)},{loudnorm},"
        f"aresample={rate},aformat=channel_layouts={layout},asetnsamples=n=1024:p=0"
    )
    limit = f"alimiter=limit={CEILING if ceiling is None else ceiling:.6g}:level=disabled"
    if not noise:
        return f"{music},{limit}[out]"
    length = int(seconds) + 2
    beds = ";".join(
        f"anoisesrc=colour={c}:sample_rate={rate}:seed={NOISE_SEEDS[c]}:duration={length},"
        f"volume={noise_gain_db(c):.2f}dB[n{c[0]}]"
        for c in ("brown", "pink", "white")
    )
    return (
        f"{music}[music];{beds};"
        f"[nb][np][nw]amix=inputs=3:normalize=0,aformat=channel_layouts={layout}[noise];"
        "[music][noise]amix=inputs=2:normalize=0:duration=first[mixed];"
        f"[mixed]{limit}[out]"
    )


def aac_command(source: Path, output: Path, graph: str, rate: int) -> list[str]:
    """Audio only. The cover art reaches the copy through copy_tags (covr).

    Mapping the art as a second stream, as the Lossless bake does, ended a
    car copy after 0.09 s: the in-graph noise sources plus the one-frame
    picture stream stop the output at once (ffmpeg 5.1, Martha Reeves' "A
    Love Like Yours", 2026-09-28; each alone was fine).
    """
    return [
        FFMPEG, "-nostdin", "-hide_banner", "-nostats", "-y", "-i", str(source),
        "-threads", "2", "-filter_complex", graph, "-map", "[out]",
        "-c:a", "aac", "-b:a", AAC_BITRATE, "-aac_pns", "0", "-ar", str(rate),
        "-map_metadata", "0", "-f", "mp4", str(output),
    ]  # fmt: skip


def bake_aac(
    source: Path, tmp_output: Path, *, noise: bool, known: Mapping[str, dict] | None = None
) -> BakeResult:
    """Bake *source* into an AAC edition copy at *tmp_output*, verified.

    The music is what lands on -14 (the loudness loudnorm reports is before
    the noise is added); the noise makes the finished file a little louder,
    as the old car edition's was.
    """
    info = probe(source)
    rate = target_rate(sample_rate_of(info))
    if rate is None:
        raise BakeError("the master's sample rate could not be read")
    try:
        seconds = float(info.get("format", {}).get("duration"))
    except (TypeError, ValueError) as exc:
        raise BakeError("the master's length could not be read") from exc
    channels = channels_of(info)
    recipe = aac_recipe(rate, channels)
    measured = _measure_or_reuse(
        recipe,
        known,
        lambda: ffmpeg_measure_loudnorm(
            source,
            AAC_TARGET_I,
            TARGET_TP,
            AAC_TARGET_LRA,
            aac_before_loudnorm(rate, channels),
            timeout=_deadline(seconds),
        ),
    )
    loud = build_second_pass_filter(measured, AAC_TARGET_I, TARGET_TP, AAC_TARGET_LRA)
    ceiling = CEILING
    for attempt in range(_PEAK_RETRIES + 1):
        graph = aac_filter(loud, rate, channels, seconds, noise, ceiling)
        proc = _run(aac_command(source, tmp_output, graph, rate), _deadline(seconds))
        if proc.returncode != 0:
            raise BakeError(f"ffmpeg exited {proc.returncode}: {(proc.stderr or '')[-200:]}")
        result = BakeResult(parse_achieved(proc.stderr), parse_mode(proc.stderr), recipe, measured)
        verify(
            info, tmp_output, result.achieved_lufs, result.mode,
            target_i=AAC_TARGET_I, rate=rate, codec="aac", max_channels=2,
            length_tolerance=REENCODE_TOLERANCE_SEC,
        )  # fmt: skip
        assert result.achieved_lufs is not None  # verify refuses a copy without it
        finished, quietest, peak = loudness_of(tmp_output, _deadline(seconds))
        # The noise only adds loudness, and in a mostly silent song (a hidden
        # track) the loudness gate counts the noise-filled silence, so the car
        # file measures UNDER the music: only louder is a fault there (cloud
        # review of #53 -- a correct copy was refused on every build).
        off = finished - result.achieved_lufs
        if off > _FINISHED_TOLERANCE or (not noise and off < -_FINISHED_TOLERANCE):
            raise BakeError(
                f"the finished copy measures {finished:.1f} LUFS; loudnorm reported "
                f"{result.achieved_lufs:.1f} for the music"
            )
        # And the noise must be there: no moment of a car copy is quieter than
        # the bed under it.
        if noise and quietest < noise_bed_lufs() - _NOISE_MARGIN:
            raise BakeError(
                f"the noise is missing: the car copy's quietest moment measures "
                f"{quietest:.1f} LUFS, the noise alone {noise_bed_lufs():.1f}"
            )
        if peak <= _PEAK_LIMIT_DBTP or attempt == _PEAK_RETRIES:
            break
        # The encoder overshot the limiter: again, lower by the overshoot.
        ceiling *= 10 ** (-(peak - _PEAK_AIM_DBTP) / 20)
    if peak > _PEAK_REFUSE_DBTP:
        raise BakeError(f"the copy still peaks at {peak:+.1f} dBTP with the limiter lowered")
    return result
