"""musaeus_overnight.sh finds the vault the way MUSAEUS does.

Cron sets no MUSAEUS_VAULT_ROOT, so the wrapper reads settings.env. It used
a built-in path to the live vault (until 2026-11-12), then its own sed
parser, which missed a byte-order mark and spaces around "=" that
musaeus/config.py accepts (CodeRabbit on #324). It now asks config.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="a bash wrapper for Linux cron")


def _vault_lines() -> str:
    """The wrapper's own lines, from SCRIPT_DIR to the vault it settles on."""
    text = (REPO / "musaeus_overnight.sh").read_text(encoding="utf-8")
    return text[text.index("SCRIPT_DIR=") : text.index("LOG_DIR=")]


def _run(home: Path, where: Path) -> subprocess.CompletedProcess[str]:
    """Run the wrapper's vault lines as a FILE, the way cron does.

    `bash -c` was the harness, and it left BASH_SOURCE[0] unset: the wrapper
    starts with SCRIPT_DIR from BASH_SOURCE. bash 5.2 fails that expansion
    softly and lands on the right folder by accident; bash 5.1 (the CI
    runner) fails it hard and the vault is never found. Running the lines
    from a file is both honest and version-proof.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("MUSAEUS_")}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"))
    # The wrapper cds to its own folder to import musaeus; here that folder is
    # a temp one, so name the package the way an install would.
    env["PYTHONPATH"] = str(REPO)
    script = "set -uo pipefail\n" + _vault_lines() + 'printf "%s" "$VAULT_ROOT"\n'
    path = where / "musaeus_overnight_slice.sh"
    path.write_text(script, encoding="utf-8")
    return subprocess.run(["bash", str(path)], cwd=REPO, env=env, capture_output=True, text=True)


def test_the_wrapper_reads_settings_as_musaeus_does(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "musaeus").mkdir(parents=True)
    vault = tmp_path / "O'Brien vault"
    line = f'﻿MUSAEUS_VAULT_ROOT = "{vault}"\n'
    (home / ".config" / "musaeus" / "settings.env").write_text(line, encoding="utf-8")
    done = _run(home, tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stderr == "", "the wrapper must not warn on its way to the vault"
    assert done.stdout == str(vault.resolve())


def test_with_no_vault_set_the_wrapper_stops(tmp_path):
    done = _run(tmp_path / "empty_home", tmp_path)
    assert done.returncode != 0
    assert "MUSAEUS_VAULT_ROOT" in done.stderr
