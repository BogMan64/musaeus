"""Review of #87, findings 1 and 14 (2026-10-07): two merge scripts predate the
masters (ALAC-Archival) layout. merge_case_duplicate_albums.py deleted a file on
a name collision with no audio check -- in a scratch vault, a different, longer
recording -- and merge_artist_folders.py moved Lossless copies, not masters, and
updated no rows. Both are retired: they refuse to run and name the maintained
tool, scripts/consolidate_artist_folders.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("script", ["merge_case_duplicate_albums.py", "merge_artist_folders.py"])
def test_a_retired_merge_script_refuses_to_run(tmp_path, script):
    env = {**os.environ, "MUSAEUS_VAULT_ROOT": str(tmp_path),
           "MUSAEUS_DB_PATH": str(tmp_path / "musaeus.db")}  # fmt: skip
    run = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), "--execute"], cwd=ROOT, env=env,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120,
    )  # fmt: skip

    assert run.returncode == 2, f"it ran: {run.stdout[-300:]} {run.stderr[-300:]}"
    assert "consolidate_artist_folders.py" in run.stderr
