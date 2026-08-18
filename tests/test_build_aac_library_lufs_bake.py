"""
Tests for the Phase 2B LUFS-baking fix in
scripts/car_library/vendor/build_aac_library.py.

Confirms MUSAEUS_OPEN_ITEMS.md's documented gap is actually closed: the
"car" profile's target_lufs (-14.0) used to be defined but never consumed
by the AAC encode -- ffmpeg ran with no loudness normalization at all.

Uses a real ffmpeg-generated, deliberately-loud synthetic source file (not
mocked) so the assertion is "the baked output actually measures near -14
LUFS", not just "ffmpeg exited 0". Invoked exactly the way
build_car_library.py invokes it in production: as a subprocess, pointed at
scratch directories via the same ORPHEUS_AAC_INPUT_DIR/ORPHEUS_AAC_OUTPUT_DIR
env-var overrides. ORPHEUS_ROOT/ORPHEUS_DB_PATH are also overridden so the
naming/metadata event logger (lib/orpheus_naming.py, unrelated to this fix)
never touches a real ORPHEUS install or its DB.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not available",
)

_VENDOR_DIR = Path(__file__).resolve().parent.parent / "scripts" / "car_library" / "vendor"


def _gen_loud_audio(path: Path, duration: int = 3) -> None:
    """A sine wave at ~0dBFS -- deliberately much louder than -14 LUFS, so a
    real bake is unambiguously required to bring it down near target."""
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration}",
        "-af",
        "volume=12dB",
        "-metadata",
        "artist=Test Artist",
        "-metadata",
        "title=Test Track",
        "-metadata",
        "album=Test Album",
        "-c:a",
        "alac",
        str(path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)


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
    data = json.loads(stderr[start : end + 1])
    return float(data["input_i"])


@pytest.fixture
def scratch(tmp_path: Path) -> dict[str, Path]:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    orpheus_root = tmp_path / "fake_orpheus_root"
    input_dir.mkdir()
    output_dir.mkdir()
    orpheus_root.mkdir()
    return {
        "input": input_dir,
        "output": output_dir,
        "orpheus_root": orpheus_root,
        "db": tmp_path / "fake_orpheus_index.db",
    }


class TestLufsBakeWiredIntoCarProfile:
    def test_baked_output_lands_near_minus_14_lufs(self, scratch: dict[str, Path]) -> None:
        src = scratch["input"] / "loud_source.m4a"
        _gen_loud_audio(src)

        # Sanity check: the *source* is well above -14 LUFS, so a passing
        # assertion on the output can only be explained by an actual bake.
        source_lufs = _measure_lufs(src)
        assert source_lufs > -13.0, f"test fixture not loud enough: {source_lufs} LUFS"

        env = {
            "PATH": __import__("os").environ.get("PATH", ""),
            "ORPHEUS_AAC_INPUT_DIR": str(scratch["input"]),
            "ORPHEUS_AAC_OUTPUT_DIR": str(scratch["output"]),
            "ORPHEUS_ROOT": str(scratch["orpheus_root"]),
            "ORPHEUS_DB_PATH": str(scratch["db"]),
        }
        result = subprocess.run(
            [
                sys.executable,
                str(_VENDOR_DIR / "build_aac_library.py"),
                "--profile",
                "car",
                "--apply",
            ],
            cwd=str(_VENDOR_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "CONVERTED |" in result.stdout, result.stdout
        assert ".bake_tmp" not in result.stdout

        outputs = list(scratch["output"].rglob("*.m4a"))
        assert len(outputs) == 1, f"expected exactly one baked output, got {outputs}"
        baked = outputs[0]

        # No leftover tmp/failed-verify artifacts.
        assert not list(scratch["output"].rglob("*.bake_tmp"))
        assert not list(scratch["output"].rglob("*.FAILED_VERIFY"))

        baked_lufs = _measure_lufs(baked)
        # loudnorm's single-pass-corrected accuracy is not exact; a wide but
        # meaningful tolerance around the -14.0 car target is enough to prove
        # baking happened (vs. source_lufs, which is >10dB higher).
        assert -16.0 <= baked_lufs <= -12.0, (
            f"baked output measured {baked_lufs} LUFS, expected ~-14"
        )

    def test_no_bake_tmp_or_failed_verify_leftover_on_success(
        self, scratch: dict[str, Path]
    ) -> None:
        src = scratch["input"] / "loud_source.m4a"
        _gen_loud_audio(src)

        env = {
            "PATH": __import__("os").environ.get("PATH", ""),
            "ORPHEUS_AAC_INPUT_DIR": str(scratch["input"]),
            "ORPHEUS_AAC_OUTPUT_DIR": str(scratch["output"]),
            "ORPHEUS_ROOT": str(scratch["orpheus_root"]),
            "ORPHEUS_DB_PATH": str(scratch["db"]),
        }
        subprocess.run(
            [
                sys.executable,
                str(_VENDOR_DIR / "build_aac_library.py"),
                "--profile",
                "car",
                "--apply",
            ],
            cwd=str(_VENDOR_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        # The verify-then-atomic-swap must leave nothing but the final .m4a
        # (and the script's own always-written CONVERSION_REPORT.txt) behind
        # -- no .bake_tmp or .FAILED_VERIFY leftovers.
        all_files = [p for p in scratch["output"].rglob("*") if p.is_file()]
        unexpected = [p for p in all_files if p.suffix not in (".m4a", ".txt")]
        assert not unexpected, unexpected
