"""
MUSAEUS — hasher subprocess handling.

Covers the stderr-drain contract in audio_hash(). ffmpeg's stdout carries the
PCM being hashed and its stderr carries diagnostics; with stderr left in an
undrained pipe, a file that makes ffmpeg emit more than the ~64 KB pipe buffer
of warnings deadlocks the whole call -- the child blocks writing stderr, so it
stops producing stdout, so the reader blocks on a stream that will never
advance. The watchdog turned that into a timeout rather than a visible hang,
so it presented as an inexplicably slow file rather than an error.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

import musaeus.hasher as hasher

# Writes ~1 MB of stderr (far past the pipe buffer) BEFORE any stdout, which
# is the exact ordering that deadlocks an undrained reader.
_FLOODING_CHILD = (
    "import sys, os\n"
    "for i in range(20000):\n"
    "    sys.stderr.write('warning line %d: damaged frame\\n' % i)\n"
    "sys.stderr.flush()\n"
    "os.write(1, b'PCMDATA' * 10000)\n"
)

_FAILING_CHILD = (
    "import sys\n"
    "for i in range(20000):\n"
    "    sys.stderr.write('error line %d\\n' % i)\n"
    "sys.stderr.flush()\n"
    "sys.exit(3)\n"
)


@pytest.fixture
def stub_ffmpeg(monkeypatch):
    """Replace the ffmpeg invocation with a Python child we control."""

    def _install(script: str) -> None:
        monkeypatch.setattr(hasher, "_probe_audio_meta", lambda path: (44100, 5.0))
        real_popen = subprocess.Popen

        def fake_popen(cmd, **kwargs):
            return real_popen([sys.executable, "-c", script], **kwargs)

        monkeypatch.setattr(hasher.subprocess, "Popen", fake_popen)

    return _install


def _run_with_timeout(fn, seconds: float = 30.0):
    """Run fn in a thread; return its result or raise on timeout.

    A plain call would hang the whole test session if the deadlock returned,
    which is precisely the failure this file exists to catch.
    """
    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 -- re-raised below
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=seconds)
    if thread.is_alive():
        pytest.fail(f"audio_hash() did not return within {seconds}s -- stderr pipe deadlock")
    if "error" in box:
        raise box["error"]
    return box["value"]


class TestStderrDrain:
    def test_heavy_stderr_does_not_deadlock(self, stub_ffmpeg, tmp_path):
        stub_ffmpeg(_FLOODING_CHILD)
        digest = _run_with_timeout(lambda: hasher.audio_hash(tmp_path / "x.flac"))
        assert len(digest) == 64
        assert digest == hasher.hashlib.sha256(b"PCMDATA" * 10000).hexdigest()

    def test_stderr_is_reported_when_the_child_fails(self, stub_ffmpeg, tmp_path):
        """The drained stderr must still reach the error message -- draining
        it concurrently must not mean losing it."""
        stub_ffmpeg(_FAILING_CHILD)
        with pytest.raises(hasher.HasherError) as excinfo:
            _run_with_timeout(lambda: hasher.audio_hash(tmp_path / "x.flac"))
        assert "exited 3" in str(excinfo.value)
        assert "error line" in str(excinfo.value)

    def test_pipes_are_closed_after_a_successful_hash(self, stub_ffmpeg, tmp_path, monkeypatch):
        """Both pipes must be closed on the way out; leaking two descriptors
        per file adds up to thousands across a library run."""
        opened: list = []
        real_popen = subprocess.Popen
        monkeypatch.setattr(hasher, "_probe_audio_meta", lambda path: (44100, 5.0))

        def tracking_popen(cmd, **kwargs):
            proc = real_popen([sys.executable, "-c", _FLOODING_CHILD], **kwargs)
            opened.append(proc)
            return proc

        monkeypatch.setattr(hasher.subprocess, "Popen", tracking_popen)

        _run_with_timeout(lambda: hasher.audio_hash(tmp_path / "x.flac"))

        assert len(opened) == 1
        proc = opened[0]
        assert proc.stdout.closed, "stdout pipe left open"
        assert proc.stderr.closed, "stderr pipe left open"


class TestFileHash:
    def test_file_hash_matches_sha256(self, tmp_path):
        path = Path(tmp_path) / "a.bin"
        path.write_bytes(b"hello musaeus")
        assert hasher.file_hash(path) == hasher.hashlib.sha256(b"hello musaeus").hexdigest()
