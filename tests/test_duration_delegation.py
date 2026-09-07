"""loudness.py and corrupt.py used to carry their own ffprobe-duration
implementations -- two of the seven independent copies found in the
2026-09-02 audit, and (for corrupt.py's ffprobe_duration) still standing
under a corrected docstring the day after that audit, caught by a semgrep
rule written the same day the docstring was fixed.

Both now delegate to musaeus.duration rather than re-implementing the
same two ffprobe calls a third and fourth time.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not available",
)


def _tone(path: Path, seconds: int = 5) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:a", "aac", "-movflags", "+faststart", str(path), "-y"],
        check=True, capture_output=True,
    )
    return path


def test_loudness_get_duration_delegates_to_container_seconds(tmp_path: Path) -> None:
    from musaeus.loudness import _get_duration

    f = _tone(tmp_path / "t.m4a")
    with patch("musaeus.loudness.container_seconds", return_value=42.0) as mocked:
        assert _get_duration(f) == 42.0
    mocked.assert_called_once_with(f)


def test_loudness_get_duration_still_works_against_a_real_file(tmp_path: Path) -> None:
    from musaeus.loudness import _get_duration

    f = _tone(tmp_path / "t.m4a", seconds=5)
    assert _get_duration(f) == pytest.approx(5.0, abs=0.5)


def test_corrupt_ffprobe_duration_delegates_to_duration_with_source(tmp_path: Path) -> None:
    from musaeus.stages.corrupt import ffprobe_duration

    f = _tone(tmp_path / "t.m4a")
    with patch(
        "musaeus.stages.corrupt.duration_with_source", return_value=(99.0, "stream")
    ) as mocked:
        assert ffprobe_duration(f) == 99.0
    mocked.assert_called_once_with(f)


def test_corrupt_ffprobe_duration_still_works_against_a_real_file(tmp_path: Path) -> None:
    from musaeus.stages.corrupt import ffprobe_duration

    f = _tone(tmp_path / "t.m4a", seconds=5)
    assert ffprobe_duration(f) == pytest.approx(5.0, abs=0.5)


def test_corrupt_ffprobe_duration_returns_none_for_a_missing_file(tmp_path: Path) -> None:
    from musaeus.stages.corrupt import ffprobe_duration

    assert ffprobe_duration(tmp_path / "nope.m4a") is None
