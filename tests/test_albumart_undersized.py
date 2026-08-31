"""Replacing art that is present but too small.

The stage asked one question -- is there art? -- and answered 99.9% coverage
while 257 files carried covers under 500px, one at 150x150. `art_quality`
could already measure that; nothing used it to TRIGGER a fetch, because the
network call sat inside the `else:` of `if has_art:` and undersized files
never reached it.

The floor passed to the sources is the file's CURRENT size, not MIN_EDGE_PX,
so a source handing back the same small image is not mistaken for an upgrade
and the audio is not rewritten for nothing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus.art_quality import MIN_EDGE_PX, image_dimensions
from musaeus.stages.albumart import _embedded_art

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="requires ffmpeg and ffprobe",
)


def _alac_with_art(path: Path, px: int) -> None:
    art = path.parent / f"art_{px}.jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=blue:s={px}x{px}:d=1",
         "-frames:v", "1", str(art)], check=True, capture_output=True)
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-i", str(art), "-map", "0:a", "-map", "1:v", "-c:a", "alac",
         "-c:v", "mjpeg", "-disposition:v:0", "attached_pic", str(path)],
        check=True, capture_output=True)


@needs_ffmpeg
def test_reports_dimensions_of_embedded_art(tmp_path: Path) -> None:
    """The probe must return the size, not just presence."""
    f = tmp_path / "small.m4a"
    _alac_with_art(f, 150)
    has, px = _embedded_art(str(f))
    assert has is True
    assert px == 150


@needs_ffmpeg
def test_undersized_art_is_distinguishable_from_good_art(tmp_path: Path) -> None:
    """150px must read as too small and 1000px must not -- this is the
    signal the replacement pass selects on."""
    small, big = tmp_path / "small.m4a", tmp_path / "big.m4a"
    _alac_with_art(small, 150)
    _alac_with_art(big, 1000)

    assert _embedded_art(str(small))[1] < MIN_EDGE_PX
    assert _embedded_art(str(big))[1] >= MIN_EDGE_PX


@needs_ffmpeg
def test_file_with_no_art_reports_zero_not_a_small_size(tmp_path: Path) -> None:
    """No art must not look like tiny art, or every artless file would be
    queued for 'replacement' instead of a plain fetch."""
    f = tmp_path / "bare.m4a"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "alac", str(f)], check=True, capture_output=True)
    assert _embedded_art(str(f)) == (False, 0)


def test_min_edge_floor_rejects_a_same_size_offer() -> None:
    """The contract the replacement relies on: asking for better than 300
    must not accept 300 back."""
    from musaeus.art_quality import is_too_small

    # A 300x300 JPEG header is 'too small' when the floor is its own size.
    blob = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=red:s=300x300:d=1",
         "-frames:v", "1", "-f", "image2", "-"],
        check=True, capture_output=True).stdout
    assert image_dimensions(blob) == (300, 300)
    assert is_too_small(blob, 301) is True
    assert is_too_small(blob, 300) is False
