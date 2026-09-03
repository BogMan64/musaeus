"""
Tests for the vendored scripts/car_library/vendor/orpheus_noise_generator.py
(salvaged from ORPHEUS, 2026-08-19 -- see scope doc §6).

Confirms real execution, not just import/syntax: generate_track() is
called directly with a short duration_min override (the production CLI
only exposes the fixed 30/60-minute TRACKS list, which would make a
routine test slow) against a real ffmpeg two-pass loudnorm pipeline --
raw noise generation, loudness measurement, AAC encode all for real.
NOISE_DIR is monkeypatched on the already-imported module (computed at
import time from ORPHEUS_NOISE_DIR normally) rather than via subprocess,
so each test gets its own scratch tmp_path without import-order games.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.car_library.vendor.orpheus_noise_generator as gen_mod  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not available",
)


def _probe(path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _measure_lufs(path: Path) -> float:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-af",
        "loudnorm=print_format=json",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    stderr = result.stderr or ""
    start, end = stderr.rfind("{"), stderr.rfind("}")
    return float(json.loads(stderr[start : end + 1])["input_i"])


class TestGenerateTrackRealExecution:
    def test_generates_real_playable_aac_at_target_lufs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gen_mod, "NOISE_DIR", tmp_path)

        ok = gen_mod.generate_track("pink", duration_min=1, track_num=1, overwrite=True)
        assert ok is True

        out = tmp_path / "Pink_Noise_1min.m4a"
        assert out.exists()

        probe = _probe(out)
        audio_streams = [s for s in probe["streams"] if s["codec_type"] == "audio"]
        assert len(audio_streams) == 1
        assert audio_streams[0]["codec_name"] == "aac"

        duration = float(probe["format"]["duration"])
        assert 55.0 <= duration <= 65.0, f"expected ~60s, got {duration}"

        lufs = _measure_lufs(out)
        assert -18.0 <= lufs <= -14.0, f"expected ~{gen_mod.TARGET_LUFS} LUFS, got {lufs}"

        # No leftover intermediate FLAC.
        assert not list(tmp_path.glob("_tmp_*.flac"))

    def test_skips_complete_file_without_overwrite(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gen_mod, "NOISE_DIR", tmp_path)
        out = tmp_path / "Brown_Noise_1min.m4a"

        assert gen_mod.generate_track("brown", duration_min=1, track_num=2, overwrite=True)
        first = out.read_bytes()

        # Second call must not spend minutes re-encoding a bed that is
        # already complete and correctly rated.
        assert gen_mod.generate_track("brown", duration_min=1, track_num=2, overwrite=False)
        assert out.read_bytes() == first

    def test_regenerates_corrupt_file_without_overwrite(self, tmp_path, monkeypatch):
        """A damaged bed must not be preserved by the skip check.

        The skip check used to be a bare out.exists(), so anything sitting at
        the output path was kept for ever -- which is how the live library
        ended up with a Pink_Noise_60min.m4a truncated to 3,064 s of an
        intended 3,600, permanently skipped by every later run.
        """
        monkeypatch.setattr(gen_mod, "NOISE_DIR", tmp_path)
        out = tmp_path / "Brown_Noise_1min.m4a"
        out.write_bytes(b"placeholder")

        assert gen_mod.generate_track("brown", duration_min=1, track_num=2, overwrite=False)
        assert out.read_bytes() != b"placeholder"
        assert gen_mod._is_good_track(out, 1)

    def test_output_rate_is_pinned_for_every_colour(self, tmp_path, monkeypatch):
        """Every bed must come out at SAMPLE_RATE, whatever loudnorm does.

        loudnorm silently ignores linear=true and falls back to dynamic mode,
        which runs the graph at 192 kHz; with no -ar the AAC encoder then
        capped at 96 kHz. Measured before the fix: pink 44,100 but brown and
        white 96,000 from the same 44,100 Hz source -- the rate depended on
        whether loudnorm happened to fall back for that colour.
        """
        monkeypatch.setattr(gen_mod, "NOISE_DIR", tmp_path)
        for colour, track_num in (("pink", 1), ("brown", 2), ("white", 3)):
            assert gen_mod.generate_track(colour, 1, track_num, overwrite=True)
            out = tmp_path / f"{colour.capitalize()}_Noise_1min.m4a"
            duration, rate = gen_mod._probe(out)
            assert rate == gen_mod.SAMPLE_RATE, f"{colour} came out at {rate} Hz"
            assert abs(duration - 60.0) <= gen_mod._DURATION_TOLERANCE_SEC

    def test_nothing_is_published_when_verification_fails(self, tmp_path, monkeypatch):
        """A bed that fails verification must not appear at the final path."""
        monkeypatch.setattr(gen_mod, "NOISE_DIR", tmp_path)
        monkeypatch.setattr(
            gen_mod, "_encode", lambda tmp, out, *a, **k: bool(out.write_bytes(b"garbage")) or True
        )

        assert gen_mod.generate_track("pink", duration_min=1, track_num=1, overwrite=True) is False
        assert not (tmp_path / "Pink_Noise_1min.m4a").exists()
        assert not list(tmp_path.glob("*.part"))
        assert not list(tmp_path.glob("_tmp_*.flac"))

    def test_overwrite_regenerates_existing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gen_mod, "NOISE_DIR", tmp_path)
        out = tmp_path / "White_Noise_1min.m4a"
        out.write_bytes(b"placeholder")

        ok = gen_mod.generate_track("white", duration_min=1, track_num=3, overwrite=True)
        assert ok is True
        assert out.read_bytes() != b"placeholder"
        probe = _probe(out)
        assert any(s["codec_type"] == "audio" for s in probe["streams"])

    def test_tmp_flac_cleaned_up_even_on_encode_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gen_mod, "NOISE_DIR", tmp_path)
        monkeypatch.setattr(gen_mod, "_encode", lambda *a, **k: False)

        ok = gen_mod.generate_track("pink", duration_min=1, track_num=1, overwrite=True)
        assert ok is False
        assert not list(tmp_path.glob("_tmp_*.flac"))
        assert not (tmp_path / "Pink_Noise_1min.m4a").exists()
