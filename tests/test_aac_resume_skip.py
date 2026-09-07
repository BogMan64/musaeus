"""Re-running an encode must resume, not start over.

There was no completed-work check at all: `convert_one` derived the output
path and went straight into a two-pass loudnorm regardless of whether that
output already existed. A re-run therefore re-encoded the entire library.

Found 2026-09-01 after a staging collision stopped a Car build at 4,858 of
10,545. Restarting it began the whole library again — roughly nine hours
to reproduce files already sitting correct on disk. The claim that it
"skips already-converted files on a re-run" was made from the shape of the
code and was simply false; it took reading `convert_one` to see there was
no such check.

Existence alone is deliberately NOT the test. A truncated file from an
interrupted run would then be preserved for ever, which is worse than
re-encoding it: the build would report success and ship a half-track. The
output must be readable and its duration must match the source, the same
thing a fresh encode is verified against.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "car_library" / "vendor"))
from build_aac_library import _output_matches_source, _probe_duration  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(
    not __import__("shutil").which("ffmpeg"), reason="requires ffmpeg"
)


def _tone(path: Path, seconds: float) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:a", "aac", "-b:a", "256k", str(path)],
        check=True, capture_output=True,
    )


@needs_ffmpeg
def test_a_complete_encode_is_recognised_and_skipped(tmp_path: Path) -> None:
    src, out = tmp_path / "src.m4a", tmp_path / "out.m4a"
    _tone(src, 5.0)
    _tone(out, 5.0)
    assert _output_matches_source(src, out) is True


@needs_ffmpeg
def test_a_truncated_encode_is_redone_not_kept(tmp_path: Path) -> None:
    """The failure that existence-only checking would cause: a half-encoded
    file preserved permanently, and the build reporting success over it."""
    src, out = tmp_path / "src.m4a", tmp_path / "out.m4a"
    _tone(src, 10.0)
    _tone(out, 3.0)
    assert _output_matches_source(src, out) is False


@needs_ffmpeg
def test_an_unreadable_output_is_redone(tmp_path: Path) -> None:
    src, out = tmp_path / "src.m4a", tmp_path / "out.m4a"
    _tone(src, 5.0)
    out.write_text("not audio at all")
    assert _output_matches_source(src, out) is False


def test_a_missing_output_is_redone(tmp_path: Path) -> None:
    assert _output_matches_source(tmp_path / "nope.m4a", tmp_path / "gone.m4a") is False


@needs_ffmpeg
def test_small_duration_drift_is_tolerated(tmp_path: Path) -> None:
    """Encoders round frame counts; a fraction of a second is not a
    truncation and must not trigger a nine-hour re-encode."""
    src, out = tmp_path / "src.m4a", tmp_path / "out.m4a"
    _tone(src, 30.0)
    _tone(out, 30.2)
    assert _output_matches_source(src, out) is True


@needs_ffmpeg
def test_probe_duration_reads_a_real_file_and_rejects_junk(tmp_path: Path) -> None:
    good = tmp_path / "good.m4a"
    _tone(good, 2.0)
    assert _probe_duration(good) == pytest.approx(2.0, abs=0.3)
    bad = tmp_path / "bad.m4a"
    bad.write_text("junk")
    assert _probe_duration(bad) is None
