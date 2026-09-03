"""
Tests for scripts/car_library/vendor/orpheus_noise_masker.py.

The masker had no test at all. These cover one thing it was getting wrong
and one it was getting right only by accident.

Wrong: resume. The skip check was a bare dst.exists(), so an output that
was truncated, or written before the output rate was pinned, was kept for
ever -- the same defect that left a permanently truncated Pink_Noise_60min
in the noise library.

Right by accident: the output rate. Nothing in the ffmpeg command stated
one, leaving it to filter-graph negotiation. Measured 2026-09-01, that
negotiation does resolve to the music's rate, including against the 96 kHz
beds in the live library -- so masked output was not in fact being inflated.
It is pinned now because it should be stated rather than inferred, and these
tests hold the behaviour either way.

Real ffmpeg throughout: what is under test is what ffmpeg does with an
under-specified command, which a mocked ffmpeg would not reproduce.
NOISE_FILES is monkeypatched on the imported module (computed at import time
from ORPHEUS_NOISE_DIR normally) so each test gets its own scratch beds.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.car_library.vendor.orpheus_noise_masker as mask_mod  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not available",
)


def _make_audio(path: Path, rate: int, seconds: float = 3.0, colour: str = "pink") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
         f"anoisesrc=colour={colour}:duration={seconds}:sample_rate={rate}",
         "-ac", "2", "-c:a", "aac", "-b:a", "128k", str(path)],
        check=True,
    )
    return path


@pytest.fixture
def beds(tmp_path, monkeypatch):
    """Noise beds at 96 kHz -- deliberately the wrong rate.

    Three of the four beds in the live library really are 96 kHz, so the
    output rate has to be decided by the music, not by whatever the beds
    happen to be. Correct beds would make this test pass for the wrong
    reason.
    """
    d = tmp_path / "noise"
    files = {
        colour: _make_audio(d / f"{colour}.m4a", 96_000, seconds=6.0, colour=colour)
        for colour in ("brown", "pink", "white")
    }
    monkeypatch.setattr(mask_mod, "NOISE_FILES", files)
    return files


def _job(src: Path, dst: Path) -> mask_mod.Job:
    return mask_mod.Job(src=src, dst=dst, brown_db=-12.0, pink_db=-15.0, white_db=-18.0)


class TestSampleRateIsPreserved:
    @pytest.mark.parametrize("rate", [44_100, 48_000])
    def test_output_matches_the_music_not_the_beds(self, tmp_path, beds, rate):
        src = _make_audio(tmp_path / "src" / "t.m4a", rate)
        dst = tmp_path / "out" / "t.m4a"

        ok, msg = mask_mod.mix_track(_job(src, dst))

        assert ok, msg
        assert mask_mod.get_sample_rate(dst) == rate, (
            f"{rate} Hz music masked against 96 kHz beds came out at "
            f"{mask_mod.get_sample_rate(dst)} Hz"
        )

    def test_duration_follows_the_music(self, tmp_path, beds):
        src = _make_audio(tmp_path / "src" / "t.m4a", 44_100, seconds=3.0)
        dst = tmp_path / "out" / "t.m4a"

        assert mask_mod.mix_track(_job(src, dst))[0]
        assert abs(mask_mod.get_duration(dst) - 3.0) <= mask_mod._DURATION_TOLERANCE_SEC


class TestResumeIsVerified:
    def test_complete_output_is_skipped(self, tmp_path, beds):
        src = _make_audio(tmp_path / "src" / "t.m4a", 44_100)
        dst = tmp_path / "out" / "t.m4a"

        assert mask_mod.mix_track(_job(src, dst))[0]
        first = dst.read_bytes()

        ok, msg = mask_mod.mix_track(_job(src, dst))
        assert ok and "SKIP" in msg
        assert dst.read_bytes() == first

    def test_output_left_at_the_wrong_rate_is_redone(self, tmp_path, beds):
        """A file masked before the rate was pinned must not be kept.

        Existence used to be the whole skip check, so every 96 kHz output
        already on disk would survive the fix untouched.
        """
        src = _make_audio(tmp_path / "src" / "t.m4a", 44_100)
        dst = _make_audio(tmp_path / "out" / "t.m4a", 96_000)

        ok, msg = mask_mod.mix_track(_job(src, dst))

        assert ok, msg
        assert "SKIP" not in msg
        assert mask_mod.get_sample_rate(dst) == 44_100

    def test_short_output_is_redone(self, tmp_path, beds):
        src = _make_audio(tmp_path / "src" / "t.m4a", 44_100, seconds=5.0)
        dst = _make_audio(tmp_path / "out" / "t.m4a", 44_100, seconds=1.0)

        ok, msg = mask_mod.mix_track(_job(src, dst))

        assert ok, msg
        assert "SKIP" not in msg
        assert abs(mask_mod.get_duration(dst) - 5.0) <= mask_mod._DURATION_TOLERANCE_SEC


class TestFailureLeavesNothingBehind:
    def test_unreadable_source_reports_failure(self, tmp_path, beds):
        src = tmp_path / "src" / "broken.m4a"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"not audio")
        dst = tmp_path / "out" / "broken.m4a"

        ok, msg = mask_mod.mix_track(_job(src, dst))

        assert not ok
        assert not dst.exists()
        assert not list((tmp_path / "out").glob("*.tmp.m4a"))
