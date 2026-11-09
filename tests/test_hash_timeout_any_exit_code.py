"""A timed-out audio decode is known by the timer, not by exit code -9.

On Windows, Popen.kill() leaves exit code 1, the same as an ffmpeg error.
A strict bit-rot check then read a slow healthy master as changed audio
and tried to replace it from a backup. Here ffmpeg is replaced by a child
that runs until it is stopped, and stopping it gives an exit code that is
not -9 (SIGTERM on Linux, 1 on Windows).
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from musaeus import hasher

_REAL_POPEN = subprocess.Popen


class _NeverEnds(_REAL_POPEN):  # type: ignore[type-arg,misc]
    """Stands in for the ffmpeg decode: prints nothing and never finishes."""

    def __init__(self, _cmd, **kwargs):
        super().__init__([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)

    def kill(self):
        self.terminate()


@pytest.fixture
def song(tmp_path, monkeypatch):
    path = tmp_path / "song.m4a"
    path.write_bytes(b"not decoded: the stand-in ignores the file")
    monkeypatch.setattr(hasher, "_probe_audio_meta", lambda _path: (44100, 1.0))
    monkeypatch.setattr(hasher, "_audio_hash_timeout", lambda *a, **k: 0)
    monkeypatch.setattr(hasher.subprocess, "Popen", _NeverEnds)
    return path


def test_a_strict_hash_that_times_out_says_timeout(song):
    with pytest.raises(hasher.HasherTimeout):
        hasher.audio_hash(song, strict=True)


def test_a_normal_hash_that_times_out_falls_back_to_the_file_hash(song):
    assert hasher.audio_hash(song) == hasher.file_hash(song)
