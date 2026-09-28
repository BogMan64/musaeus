"""build_car_library.py is retired (Grey, 2026-09-28).

The car and iPhone editions are built by `musaeus edition-build car|iphone`
into the same folders, Artist/Album/Title.m4a, with a marker in every copy.
The old builder wrote the same paths with no marker, so each file it wrote
there would block the new build ("a file with no record is in the way") and
fail the audit (cloud review of #53, findings 2 and 3). It now refuses.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "car_library" / "build_car_library.py"


def test_the_old_car_builder_refuses_and_names_its_replacement(tmp_path):
    env = {**os.environ, "MUSAEUS_VAULT_ROOT": str(tmp_path)}  # never the real vault
    env.pop("MUSAEUS_DB_PATH", None)
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--dry-run"],
        capture_output=True, text=True, timeout=60, env=env, stdin=subprocess.DEVNULL,
    )  # fmt: skip
    assert r.returncode == 2, r.stdout + r.stderr
    assert "retired" in r.stderr and "musaeus edition-build car" in r.stderr
    assert not any(tmp_path.rglob("*.m4a")), "the retired builder wrote something"
