"""Windows writes piped output in the ANSI code page (cp1252), which has no
check mark. MUSAEUS prints "✓" in every stage line, so `musaeus preflight >
log.txt` or the Companion reading MUSAEUS through a pipe died with
UnicodeEncodeError (Windows port, 2026-11-08). PYTHONIOENCODING=cp1252 makes
Linux behave like that Windows pipe."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_preflight_output_survives_a_cp1252_pipe(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "INBOX").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MUSAEUS_", "PYTHONUTF8"))}
    env.update(
        HOME=str(tmp_path / "home"),
        USERPROFILE=str(tmp_path / "home"),
        MUSAEUS_VAULT_ROOT=str(vault),
        PYTHONIOENCODING="cp1252",
        PYTHONPATH=str(ROOT),
    )
    done = subprocess.run(
        [sys.executable, "-m", "musaeus", "preflight"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        timeout=120,
    )
    err = done.stderr.decode("utf-8", "replace")
    assert "UnicodeEncodeError" not in err, err[-2000:]
    assert "✓" in done.stdout.decode("utf-8"), err[-2000:]
