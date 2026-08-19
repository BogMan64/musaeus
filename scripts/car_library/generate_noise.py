#!/usr/bin/env python3
"""
MUSAEUS — Noise Track Generator (standalone tool, NOT a pipeline stage)

(Re)generates the 30/60-minute pink/brown/white noise AAC tracks that
build_car_library.py's masking step (vendor/orpheus_noise_masker.py) and
curator.py's _find_noise_files() both already expect to find under
<vault_root>/RUNS/Noise/. Occasional/one-time prep step, not something a
normal car-library export run needs to repeat -- the noise tracks are
generic and reusable across exports, not track-specific.

Deliberately independent of `musaeus run` and the nightly cron, same as
build_car_library.py -- not imported by musaeus/stages/__init__.py or
cli.py, never invoked by the canonical pipeline.

Thin wrapper: sets ORPHEUS_NOISE_DIR to <vault_root>/RUNS/Noise (matching
build_car_library.py's own env-var-override convention for the vendored
ORPHEUS scripts) and calls vendor/orpheus_noise_generator.py directly.

Usage:
    python3 scripts/car_library/generate_noise.py               # dry run
    python3 scripts/car_library/generate_noise.py --apply
    python3 scripts/car_library/generate_noise.py --apply --overwrite
    python3 scripts/car_library/generate_noise.py --apply --30-only
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from musaeus.config import get_config  # noqa: E402

VENDOR_DIR = Path(__file__).resolve().parent / "vendor"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MUSAEUS Noise Track Generator -- pink/brown/white noise for car-library masking"
    )
    parser.add_argument("--apply", action="store_true", help="Generate files (default: dry-run)")
    parser.add_argument(
        "--overwrite", action="store_true", help="Regenerate files that already exist"
    )
    parser.add_argument(
        "--30-only", dest="thirty_only", action="store_true", help="Only 30-minute tracks"
    )
    args = parser.parse_args()

    cfg = get_config()
    noise_dir = cfg.runs_root / "Noise"

    env = os.environ.copy()
    env["ORPHEUS_NOISE_DIR"] = str(noise_dir)

    cmd = [sys.executable, str(VENDOR_DIR / "orpheus_noise_generator.py")]
    if args.apply:
        cmd.append("--apply")
    if args.overwrite:
        cmd.append("--overwrite")
    if args.thirty_only:
        cmd.append("--30-only")

    result = subprocess.run(cmd, cwd=str(VENDOR_DIR), env=env)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
