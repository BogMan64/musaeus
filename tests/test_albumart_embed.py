"""`_embed_art`'s ffmpeg invocation, against the real binary.

The temp file was named `src.with_suffix(src.suffix + ".artmp")` -- i.e.
`track.m4a.artmp`. ffmpeg picks its muxer from the output extension, and
`.artmp` is not one, so every call died with

    Unable to find a suitable output format for '....m4a.artmp'

returned non-zero, and `_embed_art` returned False. The stage logged a
warning per file and carried on, so the failure was survivable and never
loud. `ART_EMBEDDED` had never once been written to the event log against
a library of 10,554 catalogued rows; the sidecar-embed path had in fact
never worked at all.

It stayed invisible because nothing exercised the ffmpeg call: before
this file, `_embed_art` appeared nowhere in tests/. The surrounding
selection and fetch logic was covered, which is what made the stage look
tested. Found 2026-08-31 by running the stage over the library and
noticing that every fetch was followed by "embed failed".

These tests run real ffmpeg on a real ALAC file for that reason -- a mock
would have passed against the broken command.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus.stages.albumart import _embed_art

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="requires ffmpeg and ffprobe",
)


def _make_alac(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "alac", str(path)],
        check=True, capture_output=True,
    )


def _make_jpeg(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=red:s=600x600:d=1",
         "-frames:v", "1", str(path)],
        check=True, capture_output=True,
    )


def _streams(path: Path) -> list[tuple[str, str]]:
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
         "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    )
    out = []
    for line in res.stdout.strip().splitlines():
        name, kind = line.strip().rstrip(",").split(",")
        out.append((kind, name))
    return out


@needs_ffmpeg
def test_embed_art_actually_embeds(tmp_path: Path) -> None:
    """The whole point of the stage: art goes in, ffmpeg agrees it is there."""
    audio = tmp_path / "track.m4a"
    art = tmp_path / "cover.jpg"
    _make_alac(audio)
    _make_jpeg(art)

    assert _streams(audio) == [("audio", "alac")]

    assert _embed_art(str(audio), art) is True

    streams = _streams(audio)
    assert ("audio", "alac") in streams, "ALAC audio must survive the rewrite"
    assert ("video", "mjpeg") in streams, "cover art must be present after embed"


@needs_ffmpeg
def test_embed_art_leaves_no_temp_file(tmp_path: Path) -> None:
    """The temp file is renamed over the original, never left beside it."""
    audio = tmp_path / "track.m4a"
    art = tmp_path / "cover.jpg"
    _make_alac(audio)
    _make_jpeg(art)

    _embed_art(str(audio), art)

    leftovers = [p.name for p in tmp_path.iterdir() if ".artmp" in p.name]
    assert leftovers == [], f"temp files left behind: {leftovers}"


@needs_ffmpeg
def test_embed_art_preserves_original_when_ffmpeg_fails(tmp_path: Path) -> None:
    """A failed embed must not damage the audio -- art is a nicety, the file is not."""
    audio = tmp_path / "track.m4a"
    _make_alac(audio)
    before = audio.read_bytes()

    not_an_image = tmp_path / "cover.jpg"
    not_an_image.write_text("this is not a JPEG")

    assert _embed_art(str(audio), not_an_image) is False
    assert audio.read_bytes() == before, "original audio was modified by a failed embed"
    assert [p.name for p in tmp_path.iterdir() if ".artmp" in p.name] == []
