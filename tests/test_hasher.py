"""The hasher's subprocess handling.

`audio_hash` reads ffmpeg's PCM off stdout while ffmpeg writes diagnostics
to stderr. Both are pipes, and a pipe whose reader never reads blocks its
writer once the kernel buffer fills (~64 KB on Linux).

The old order read stdout to exhaustion and only THEN read stderr, so a
verbose ffmpeg blocked writing stderr, stopped producing stdout, and
proc.wait() never returned. Only the kill-timer eventually freed it, and
that timer scales with track duration -- so the stall grew with the file.
ffmpeg gets more verbose on a damaged file, which is exactly when hashing
matters.

Reproduced 2026-08-30 against a child emitting 2 MB of stderr: still
blocked after 20 s. With stderr drained concurrently: 0.10 s.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

import pytest

from musaeus.hasher import HasherError, audio_hash

_FLOOD_BYTES = 2_000_000
_DEADLOCK_TIMEOUT = 15


def _fake_ffmpeg(directory: Path, body: str) -> None:
    """Put a stand-in `ffmpeg` (and `ffprobe`) at the front of PATH."""
    for name in ("ffmpeg", "ffprobe"):
        exe = directory / name
        exe.write_text("#!/usr/bin/env python3\n" + body)
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ['PATH']}"


def _run_with_deadlock_guard(fn, timeout=_DEADLOCK_TIMEOUT):
    """Run *fn* on a thread; report whether it finished. A hang is the bug."""
    box: dict = {}

    def _target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            box["error"] = exc

    t = threading.Thread(target=_target, daemon=True)
    started = time.time()
    t.start()
    t.join(timeout=timeout)
    return (not t.is_alive()), box, time.time() - started


@pytest.fixture
def flooding_ffmpeg(tmp_path, monkeypatch):
    """An ffmpeg that emits a little stdout and a great deal of stderr."""
    monkeypatch.setenv("PATH", os.environ["PATH"])
    _fake_ffmpeg(
        tmp_path,
        "import sys\n"
        "sys.stdout.buffer.write(b'A' * 1024)\n"
        "sys.stdout.buffer.flush()\n"
        f"sys.stderr.write('x' * {_FLOOD_BYTES})\n"
        "sys.stderr.flush()\n",
    )
    src = tmp_path / "song.m4a"
    src.write_bytes(b"placeholder")
    return src


def test_a_flood_of_stderr_does_not_deadlock(flooding_ffmpeg):
    """THE regression. Without concurrent draining this never returns."""
    finished, box, elapsed = _run_with_deadlock_guard(
        lambda: audio_hash(flooding_ffmpeg)
    )
    assert finished, (
        f"audio_hash deadlocked: still blocked after {elapsed:.0f}s with "
        f"{_FLOOD_BYTES} bytes on stderr"
    )
    assert "error" not in box, box.get("error")
    assert isinstance(box["value"], str) and len(box["value"]) == 64


def test_the_hash_is_of_stdout_not_stderr(flooding_ffmpeg):
    """Draining stderr must not let it contaminate the digest."""
    import hashlib

    finished, box, _ = _run_with_deadlock_guard(lambda: audio_hash(flooding_ffmpeg))
    assert finished
    assert box["value"] == hashlib.sha256(b"A" * 1024).hexdigest()


def test_a_failing_ffmpeg_still_reports_its_stderr(tmp_path, monkeypatch):
    """The diagnostic must survive being drained on a thread."""
    monkeypatch.setenv("PATH", os.environ["PATH"])
    _fake_ffmpeg(
        tmp_path,
        "import sys\n"
        "sys.stderr.write('Invalid data found when processing input')\n"
        "sys.exit(1)\n",
    )
    src = tmp_path / "broken.m4a"
    src.write_bytes(b"placeholder")

    finished, box, elapsed = _run_with_deadlock_guard(lambda: audio_hash(src))
    assert finished, f"blocked after {elapsed:.0f}s on the failure path"
    assert isinstance(box.get("error"), HasherError)
    assert "Invalid data found" in str(box["error"])


def test_a_silent_failing_ffmpeg_is_still_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", os.environ["PATH"])
    _fake_ffmpeg(tmp_path, "import sys\nsys.exit(3)\n")
    src = tmp_path / "quiet.m4a"
    src.write_bytes(b"placeholder")

    finished, box, _ = _run_with_deadlock_guard(lambda: audio_hash(src))
    assert finished
    assert isinstance(box.get("error"), HasherError)


def test_missing_ffmpeg_raises_rather_than_hanging(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    src = tmp_path / "song.m4a"
    src.write_bytes(b"placeholder")

    finished, box, _ = _run_with_deadlock_guard(lambda: audio_hash(src))
    assert finished
    assert isinstance(box.get("error"), HasherError)
