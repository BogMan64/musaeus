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

    def test_skips_existing_file_without_overwrite(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gen_mod, "NOISE_DIR", tmp_path)
        out = tmp_path / "Brown_Noise_1min.m4a"
        out.write_bytes(b"placeholder")

        ok = gen_mod.generate_track("brown", duration_min=1, track_num=2, overwrite=False)
        assert ok is True
        # Untouched -- real generation would have overwritten this with a
        # real AAC file, not left the placeholder bytes in place.
        assert out.read_bytes() == b"placeholder"

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
