"""A timed-out audio decode is known by the timer, not by exit code -9.

On Windows, Popen.kill() leaves exit code 1, the same as an ffmpeg error.
A strict bit-rot check then read a slow healthy master as changed audio
and tried to replace it from a backup. Here kill() is swapped for
terminate(), which on Linux also gives an exit code that is not -9.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from musaeus import hasher

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")


@pytest.fixture
def song(tmp_path, monkeypatch):
    path = tmp_path / "song.m4a"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=d=2",
            "-c:a",
            "alac",
            str(path),
        ],
        check=True,
    )
    monkeypatch.setattr(hasher, "_audio_hash_timeout", lambda *a, **k: 0)
    monkeypatch.setattr(subprocess.Popen, "kill", subprocess.Popen.terminate)
    return path


def test_a_strict_hash_that_times_out_says_timeout(song):
    with pytest.raises(hasher.HasherTimeout):
        hasher.audio_hash(song, strict=True)


def test_a_normal_hash_that_times_out_falls_back_to_the_file_hash(song):
    assert hasher.audio_hash(song) == hasher.file_hash(song)
