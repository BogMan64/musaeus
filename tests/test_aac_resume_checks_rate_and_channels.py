"""The resume check must ask "is this what the current settings produce?"

M-02 in the Repair Register, 2026-09-08.

`_output_matches_source()` compared duration and nothing else. That is
correct for the failure it was written against — a truncated file from an
interrupted run — and silently wrong for the one that came later.

When the encoder gained the `-ar` cap and the `-ac 2` downmix, every output
made before them kept exactly the right *duration* while having the wrong
rate and channel count. Each one therefore reported
`SKIP DONE | already encoded` and was never revisited. By `car_sample_rate`'s
own measurement that is **4,862 of 10,545 files above 48 kHz, 4,223 of them
at 192 kHz** — the files the cap was added FOR, held out of reach of the fix
by the check meant to protect them.

The failure has the shape this project keeps meeting: a guard that returns a
confident answer to a question slightly different from the one being asked.

Two design points, both load-bearing:

**A property is judged only when it can be measured.** An unreadable probe
leaves that property alone rather than failing the file, because a False
here means *delete and re-encode*, and deleting on a measurement we could
not take is M-01. Duration stays mandatory — without it there is no check.

**Mono stays mono.** `-ac 2` fires only above two channels, so the
expectation is "2 if the source is surround, otherwise unchanged". Asserting
a flat stereo expectation would re-encode every genuinely mono recording in
the library for ever, and this library is full of them.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "car_library" / "vendor"))
from build_aac_library import (  # noqa: E402
    _output_matches_source,
    _probe_rate_and_channels,
    car_sample_rate,
)

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="requires ffmpeg/ffprobe",
)


def _tone(path: Path, seconds: float, rate: int = 44_100, channels: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    layout = {1: "mono", 2: "stereo", 6: "5.1"}[channels]
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}:sample_rate={rate}",
            "-af",
            f"aformat=channel_layouts={layout}",
            "-c:a",
            "aac",
            "-ar",
            str(rate),
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@needs_ffmpeg
def test_the_probe_reads_both_fields_in_one_call(tmp_path: Path) -> None:
    f = _tone(tmp_path / "a.m4a", 2.0, rate=48_000, channels=2)
    assert _probe_rate_and_channels(f) == (48_000, 2)


def test_the_probe_fails_soft_on_junk(tmp_path: Path) -> None:
    junk = tmp_path / "junk.m4a"
    junk.write_text("not audio")
    assert _probe_rate_and_channels(junk) == (None, None)
    assert _probe_rate_and_channels(tmp_path / "missing.m4a") == (None, None)


# ── the finding ──────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_a_96k_output_from_a_96k_source_is_redone(tmp_path: Path) -> None:
    """The exact M-02 case: right duration, wrong rate, previously SKIP DONE."""
    source = _tone(tmp_path / "src.m4a", 5.0, rate=96_000)
    stale = _tone(tmp_path / "out.m4a", 5.0, rate=96_000)  # encoded before the cap

    assert car_sample_rate(96_000) == 48_000, "policy: 96k caps to 48k"
    assert _output_matches_source(source, stale) is False, (
        "a pre-cap output must not report already-encoded"
    )


@needs_ffmpeg
def test_the_capped_output_of_that_source_is_kept(tmp_path: Path) -> None:
    """And the corrected encode must then be left alone, or it re-encodes for ever."""
    source = _tone(tmp_path / "src.m4a", 5.0, rate=96_000)
    good = _tone(tmp_path / "out.m4a", 5.0, rate=48_000)
    assert _output_matches_source(source, good) is True


@needs_ffmpeg
def test_a_surround_source_with_a_surround_output_is_redone(tmp_path: Path) -> None:
    source = _tone(tmp_path / "src.m4a", 4.0, rate=48_000, channels=6)
    stale = _tone(tmp_path / "out.m4a", 4.0, rate=48_000, channels=6)
    assert _output_matches_source(source, stale) is False


@needs_ffmpeg
def test_a_surround_source_downmixed_to_stereo_is_kept(tmp_path: Path) -> None:
    source = _tone(tmp_path / "src.m4a", 4.0, rate=48_000, channels=6)
    good = _tone(tmp_path / "out.m4a", 4.0, rate=48_000, channels=2)
    assert _output_matches_source(source, good) is True


# ── the two things the fix must NOT break ────────────────────────────────────


@needs_ffmpeg
def test_a_mono_source_stays_mono_and_is_not_re_encoded_for_ever(tmp_path: Path) -> None:
    """-ac 2 fires only above two channels. This library is full of mono."""
    source = _tone(tmp_path / "src.m4a", 4.0, rate=44_100, channels=1)
    out = _tone(tmp_path / "out.m4a", 4.0, rate=44_100, channels=1)
    assert _output_matches_source(source, out) is True


@needs_ffmpeg
def test_an_ordinary_44k_stereo_pair_still_resumes(tmp_path: Path) -> None:
    """The whole point of the check: do not redo nine hours of correct work."""
    source = _tone(tmp_path / "src.m4a", 6.0, rate=44_100, channels=2)
    out = _tone(tmp_path / "out.m4a", 6.0, rate=44_100, channels=2)
    assert _output_matches_source(source, out) is True


@needs_ffmpeg
def test_a_truncated_output_is_still_caught(tmp_path: Path) -> None:
    """Duration remains mandatory -- the original reason this check exists."""
    source = _tone(tmp_path / "src.m4a", 30.0)
    out = _tone(tmp_path / "out.m4a", 2.0)
    assert _output_matches_source(source, out) is False


@needs_ffmpeg
def test_an_unmeasurable_rate_does_not_condemn_a_good_output(tmp_path: Path) -> None:
    """M-01's rule, restated: never delete on a measurement we could not take.

    car_sample_rate(None) is None -- "unreadable: do not guess" -- so the rate
    is left unjudged and the duration match carries the decision.
    """
    assert car_sample_rate(None) is None
    source = _tone(tmp_path / "src.m4a", 5.0, rate=44_100)
    out = _tone(tmp_path / "out.m4a", 5.0, rate=44_100)
    assert _output_matches_source(source, out) is True
