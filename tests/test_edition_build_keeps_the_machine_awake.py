"""An edition build holds a sleep inhibitor (cloud review of #53, finding 5).

Retiring build_car_library.py also dropped its systemd-inhibit: the 19-hour
car build ran with nothing keeping the machine awake, and the idle throttle
needs the screen-saver's keep-awake off (musaeus/sleep_inhibit.py). The
re-exec must also keep `python3 -m musaeus.cli`: run again as a script path,
the child would fail on its first relative import.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from musaeus import sleep_inhibit

ROOT = Path(__file__).resolve().parents[1]


def test_the_reexec_keeps_python_dash_m(monkeypatch):
    seen: list[list[str]] = []

    class Replaced(Exception):
        pass

    def fake_execve(exe, argv, env):
        seen.append(list(argv))
        raise Replaced

    monkeypatch.delenv(sleep_inhibit._GUARD, raising=False)
    monkeypatch.delenv(sleep_inhibit._DISABLE, raising=False)
    monkeypatch.setattr(sleep_inhibit.shutil, "which", lambda name: "/usr/bin/systemd-inhibit")
    monkeypatch.setattr(
        sleep_inhibit.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0)
    )
    monkeypatch.setattr(sleep_inhibit.os, "execve", fake_execve)
    monkeypatch.setattr(sys, "argv", ["/x/musaeus/cli.py", "edition-build", "car"])
    monkeypatch.setattr(
        sys, "orig_argv", ["python3", "-m", "musaeus.cli", "edition-build", "car"], raising=False
    )
    with pytest.raises(Replaced):
        sleep_inhibit.reexec_under_inhibitor("car edition build")
    assert seen[0][-5:] == [sys.executable, "-m", "musaeus.cli", "edition-build", "car"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")
def test_a_real_build_holds_the_inhibitor_and_a_dry_run_does_not(tmp_path):
    from musaeus.db import open_db, upsert_archive

    master = tmp_path / "Libraries" / "ALAC-Archival" / "Rock" / "A" / "Al" / "A - T.m4a"
    master.parent.mkdir(parents=True)
    master.write_bytes(b"x")
    conn = open_db(tmp_path / "musaeus.db")
    upsert_archive(conn, {"file_path": str(master), "status": "CATALOGUED",
                          "audio_hash": "h1", "codec": "alac"})  # fmt: skip
    conn.commit()
    conn.close()

    # A stand-in systemd-inhibit: agrees to the probe, logs the real call,
    # then runs the command after --mode=block -- as the real one does.
    bin_dir, log = tmp_path / "bin", tmp_path / "inhibit.log"
    bin_dir.mkdir()
    fake = bin_dir / "systemd-inhibit"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$*" in *--why=probe*) exit 0;; esac\n'
        f'echo "$*" >> "{log}"\n'
        'while [ "$1" != "--mode=block" ]; do shift; done; shift\n'
        'exec "$@"\n'
    )
    fake.chmod(0o755)
    env = {**os.environ, "MUSAEUS_VAULT_ROOT": str(tmp_path),
           "MUSAEUS_DB_PATH": str(tmp_path / "musaeus.db"),
           "PATH": f"{bin_dir}:{os.environ['PATH']}"}  # fmt: skip
    env.pop(sleep_inhibit._GUARD, None)
    env.pop(sleep_inhibit._DISABLE, None)

    def run(*extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "musaeus.cli", "edition-build", "car", *extra],
            capture_output=True, text=True, env=env, cwd=ROOT, stdin=subprocess.DEVNULL,
            timeout=120,
        )  # fmt: skip

    dry = run("--dry-run")
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert not log.exists(), "a dry run took the inhibitor"
    real = run("--limit", "0")
    assert real.returncode == 0, real.stdout + real.stderr
    assert "--what=sleep:idle" in log.read_text(), "the build ran without the inhibitor"
    assert "Car edition" in real.stdout, "the inhibited child did not run the build"
    assert sqlite3.connect(tmp_path / "musaeus.db").execute("SELECT 1").fetchone()
