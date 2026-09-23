"""Masking must not throw the album art away.

Found mid-run, 2026-09-16. The masker re-encodes the audio and writes a fresh
container, mapping only the filter graph's [out] -- which is audio. Sampled
while the run was in progress: 12 of 12 SOURCES had embedded art and 0 of 12
MASKED OUTPUTS did. 5,103 files had already been written that way and had to
be discarded.

The scale of what was nearly lost: ALAC-Archival is 11,408 of 11,408 with art
and ALAC_Library 11,389 of 11,389 -- both 100%. Masking was the single step
that dropped it, and the car edition is the copy that actually ships, so the
one version Grey would ever see in the car would have been the one with no
artwork.

WHY A TAG COPY AND NOT AN FFMPEG STREAM MAP

The obvious fix was tried first:

    -map 0:v? -c:v copy -disposition:v:0 attached_pic

It produced a 0.09-SECOND FILE. The muxer finalised on the single-frame
attached picture instead of the audio. The masker's own verify step rejected
both test files, which is exactly the job that step exists to do -- ffmpeg
exited 0 and had written something useless.

So the art is copied as a TAG after the encode, where it cannot influence
duration.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "car_library" / "vendor"))

import orpheus_noise_masker as M  # noqa: E402

mutagen_mp4 = pytest.importorskip("mutagen.mp4")


def _m4a(path: Path, art: bytes | None = None) -> Path:
    """A real, tiny m4a -- mutagen refuses to open a fabricated one."""
    import subprocess

    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "quiet",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "1",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )
    if art is not None:
        f = mutagen_mp4.MP4(path)
        if f.tags is None:
            f.add_tags()
        f.tags["covr"] = [mutagen_mp4.MP4Cover(art, imageformat=mutagen_mp4.MP4Cover.FORMAT_JPEG)]
        f.save()
    return path


class TestTheArtIsCarried:
    def test_cover_bytes_arrive_unchanged(self, tmp_path):
        art = b"\xff\xd8\xff" + b"J" * 500  # a JPEG-looking blob
        src = _m4a(tmp_path / "src.m4a", art)
        dst = _m4a(tmp_path / "dst.m4a", None)
        M._carry_cover_art(src, dst)
        tags = mutagen_mp4.MP4(dst).tags
        assert tags is not None and "covr" in tags
        assert bytes(tags["covr"][0]) == art, "the cover was altered in transit"

    def test_an_existing_cover_on_the_output_is_replaced(self, tmp_path):
        src = _m4a(tmp_path / "src.m4a", b"\xff\xd8\xffNEW" + b"N" * 200)
        dst = _m4a(tmp_path / "dst.m4a", b"\xff\xd8\xffOLD" + b"O" * 200)
        M._carry_cover_art(src, dst)
        assert b"NEW" in bytes(mutagen_mp4.MP4(dst).tags["covr"][0])


class TestItNeverCostsATrack:
    """Losing artwork is not a reason to fail a track that is otherwise fine."""

    def test_a_source_with_no_art_leaves_the_output_alone(self, tmp_path):
        src = _m4a(tmp_path / "src.m4a", None)
        dst = _m4a(tmp_path / "dst.m4a", None)
        M._carry_cover_art(src, dst)  # must not raise
        assert dst.is_file()

    def test_an_unreadable_source_is_not_an_error(self, tmp_path):
        bad = tmp_path / "bad.m4a"
        bad.write_bytes(b"not an mp4 at all")
        dst = _m4a(tmp_path / "dst.m4a", None)
        M._carry_cover_art(bad, dst)  # must not raise
        assert dst.is_file()

    def test_a_missing_destination_is_not_an_error(self, tmp_path):
        src = _m4a(tmp_path / "src.m4a", b"\xff\xd8\xff" + b"x" * 100)
        M._carry_cover_art(src, tmp_path / "gone.m4a")  # must not raise


class TestTheOrderingItRunsIn:
    def test_the_art_is_copied_after_verify_and_before_the_rename(self):
        """After verify, so a file that failed its duration check is never
        touched. Before the rename, so what lands at the destination is
        complete -- 'existence is not completeness'."""
        src = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "car_library"
            / "vendor"
            / "orpheus_noise_masker.py"
        ).read_text()
        verify = src.index("if not _output_is_complete(")
        carry = src.index("_carry_cover_art(job.src, tmp)")
        rename = src.index("tmp.rename(job.dst)")
        assert verify < carry < rename

    def test_the_filter_graph_does_not_map_the_art(self):
        """`-map 0:v? -c:v copy -disposition:v:0 attached_pic` made the muxer
        finalise on the one-frame image and wrote a 0.09-second file. If this
        ever comes back, the duration goes with it."""
        src = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "car_library"
            / "vendor"
            / "orpheus_noise_masker.py"
        ).read_text()
        start = src.index("cmd = [")
        block = src[start : src.index("]", src.index("str(tmp)", start))]
        # Strip comment lines. The block explains WHY the stream map was
        # rejected, so the rejected flags appear in the prose -- and an
        # earlier version of this test compared two pieces of documentation
        # and failed. Assert on the code.
        cmd = "\n".join(ln for ln in block.splitlines() if not ln.strip().startswith("#"))
        assert '"0:v?"' not in cmd, "mapping the art through the graph breaks the duration"
        assert "attached_pic" not in cmd
