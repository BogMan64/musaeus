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
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:a",
            "alac",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_jpeg(path: Path) -> None:
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
            "color=c=red:s=600x600:d=1",
            "-frames:v",
            "1",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _streams(path: Path) -> list[tuple[str, str]]:
    res = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
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


# ── Review of slice A (#143), findings 1 and 2, both verified by running: the
# embed remuxed the whole master through ffmpeg, which read the terminal (a
# "q" typed during a run replaced a 10 MB master with a 258-byte file) and
# dropped every freeform tag on the way. Now mutagen adds the cover to a copy
# beside the file (musaeus.safe_save), and the copy replaces it once checked. ──


@needs_ffmpeg
def test_embedding_a_cover_keeps_every_other_tag(tmp_path: Path) -> None:
    from mutagen.mp4 import MP4, MP4FreeForm

    audio = tmp_path / "track.m4a"
    art = tmp_path / "cover.jpg"
    _make_alac(audio)
    _make_jpeg(art)
    f = MP4(audio)
    f.tags["----:com.apple.iTunes:R128_TRACK_GAIN"] = [MP4FreeForm(b"-1280")]
    f.tags["----:com.apple.iTunes:MusicBrainz Track Id"] = [MP4FreeForm(b"abc-123")]
    f.tags["tmpo"] = [120]
    f.tags["soar"] = ["Stones, The"]
    f.save()
    before = dict(MP4(audio).tags)

    assert _embed_art(str(audio), art) is True

    after = MP4(audio).tags
    assert after.get("covr"), "no cover"
    for key, value in before.items():
        assert after.get(key) == value, f"{key} lost or changed by the embed"


@needs_ffmpeg
def test_embedding_never_runs_ffmpeg_on_the_master(tmp_path: Path, monkeypatch) -> None:
    """No subprocess: nothing can read the terminal, and nothing remuxes."""
    import musaeus.stages.albumart as albumart

    audio = tmp_path / "track.m4a"
    art = tmp_path / "cover.jpg"
    _make_alac(audio)
    _make_jpeg(art)

    def no_subprocess(*args, **kwargs):
        raise AssertionError("ffmpeg was run on the master")

    monkeypatch.setattr(albumart.subprocess, "run", no_subprocess)
    assert _embed_art(str(audio), art) is True


@needs_ffmpeg
def test_a_save_that_fails_leaves_the_master_as_it_was(tmp_path: Path, monkeypatch) -> None:
    from mutagen.mp4 import MP4

    audio = tmp_path / "track.m4a"
    art = tmp_path / "cover.jpg"
    _make_alac(audio)
    _make_jpeg(art)
    before = audio.read_bytes()

    def cut_short(self, *args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(MP4, "save", cut_short)
    assert _embed_art(str(audio), art) is False
    assert audio.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cover.jpg", "track.m4a"]
