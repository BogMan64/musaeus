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


def ffmpeg_measure_loudnorm(
    path: Path,
    target_i: str = TARGET_I,
    target_tp: str = TARGET_TP,
    target_lra: str = TARGET_LRA,
    before: str = "",
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
        _BAKE_TIMEOUT,
    )
    err = proc.stderr or ""
    start, end = err.rfind("{"), err.rfind("}")
    if start == -1 or end <= start:
        raise BakeError(f"could not read the loudness measurement of {path.name}")
    measured: dict = json.loads(err[start : end + 1])
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
) -> None:
    """The copy has audio, the right rate, its length, and the target loudness.

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
        return source_rate
    return 44_100 if source_rate % 44_100 == 0 else 48_000


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


def aac_filter(loudnorm: str, rate: int, channels: int, seconds: float, noise: bool) -> str:
    """The one filter graph: music at the target, noise under it, limited."""
    # The layout is STATED on both sides of the mix. Left to negotiation,
    # loudnorm plus the three-colour mix collapsed a stereo master to mono
    # (measured 2026-09-28; each part alone stayed stereo) -- CLAUDE.md: an
    # unstated format property is decided by the input. The rate is stated
    # again after loudnorm, which always outputs 192 kHz.
    layout = _layout(channels)
    music = (
        f"[0:a:0]{aac_before_loudnorm(rate, channels)},{loudnorm},"
        f"aresample={rate},aformat=channel_layouts={layout}"
    )
    limit = f"alimiter=limit={CEILING}:level=disabled"
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
        "-c:a", "aac", "-b:a", AAC_BITRATE, "-ar", str(rate),
        "-map_metadata", "0", "-f", "mp4", str(output),
    ]  # fmt: skip


def bake_aac(source: Path, tmp_output: Path, *, noise: bool) -> BakeResult:
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
    measured = ffmpeg_measure_loudnorm(
        source, AAC_TARGET_I, TARGET_TP, AAC_TARGET_LRA, aac_before_loudnorm(rate, channels)
    )
    loud = build_second_pass_filter(measured, AAC_TARGET_I, TARGET_TP, AAC_TARGET_LRA)
    graph = aac_filter(loud, rate, channels, seconds, noise)
    proc = _run(aac_command(source, tmp_output, graph, rate), _BAKE_TIMEOUT)
    if proc.returncode != 0:
        raise BakeError(f"ffmpeg exited {proc.returncode}: {(proc.stderr or '')[-200:]}")
    result = BakeResult(parse_achieved(proc.stderr), parse_mode(proc.stderr))
    verify(
        info, tmp_output, result.achieved_lufs, result.mode,
        target_i=AAC_TARGET_I, rate=rate, codec="aac", max_channels=2,
    )  # fmt: skip
    return result
