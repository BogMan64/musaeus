"""No tracked file names a folder of the machine running the tests.

The repository is public. Until 2026-11-12 it named its maintainer's home,
drives and mount points about 220 times; they were replaced by settings and
placeholders. This keeps them out. The folders are worked out on the machine
(tests/real_roots.py), so this file names nobody's.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.real_roots import REAL_BACKUP_ROOT, REAL_HOME, REAL_SECRETS_DIR, REAL_VAULT

REPO = Path(__file__).resolve().parents[1]


# shortcut: checks the folders of the machine running it (CI checks the runner's), so
# it protects the maintainer's machine, where the full suite runs before each push.
def _this_machines_folders() -> list[str]:
    folders = [str(REAL_HOME), f"/media/{REAL_HOME.name}/"]
    if REAL_VAULT is not None:
        folders.append(str(Path(*REAL_VAULT.parts[:3])))  # the vault's drive, e.g. /mnt/<disk>
    folders += [str(p) for p in (REAL_BACKUP_ROOT, REAL_SECRETS_DIR) if p is not None]
    return folders


def test_no_tracked_file_names_this_machines_folders():
    hits = []
    for folder in _this_machines_folders():
        found = subprocess.run(
            ["git", "grep", "-IlF", folder], cwd=REPO, capture_output=True, text=True
        )
        if found.returncode not in (0, 1):  # 1 = no match
            pytest.skip("not a git checkout (the Windows trial kit is a plain copy)")
        hits += [f"{f}: {folder}" for f in found.stdout.split("\n") if f]
    assert not hits, "personal folders in tracked files:\n" + "\n".join(hits)
