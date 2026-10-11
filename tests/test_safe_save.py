"""Every stage writer that tags a master works on a copy beside it
(musaeus.safe_save.write_beside): a save cut short leaves the master as it
was. Review of slice A (#143), findings 4-6: Forge, Tagger, BPM and
IdentityTag saved masters in place, so a kill, power cut or full disk mid-save
left a damaged master under its final name.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.mp4 import MP4

from musaeus import identity_tags
from musaeus.hasher import audio_hash
from musaeus.stages import bpm, forge, tagger

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg")

FEATURES = {"bpm": 120.0, "musical_key": "C", "energy": 0.5, "danceability": 0.5}

WRITERS = {
    "forge": lambda p: forge._write_tags_m4a(p, -5.0, 0.9),
    "bpm": lambda p: bpm._write_tags_m4a(p, FEATURES),
    "identity": lambda p: identity_tags.write_identity(p, {"mb_artist_id": "abc-123"})[0],
    "tagger": lambda p: tagger._write_tags(p, {"title": "Brown Sugar (2009 Remaster)"}),
}


def _m4a(path: Path) -> Path:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1", "-c:a", "alac", str(path)],
                   check=True, capture_output=True)  # fmt: skip
    return path


@needs_ffmpeg
@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_a_save_cut_short_leaves_the_master_as_it_was(tmp_path, monkeypatch, writer):
    master = _m4a(tmp_path / "song.m4a")
    before = master.read_bytes()

    def cut_short(self, *args, **kwargs):
        name = args[0] if args else getattr(self, "filename", None)
        with open(name, "r+b") as fh:
            fh.truncate(100)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(MP4, "save", cut_short)
    assert not WRITERS[writer](master), "a failed save reported success"
    assert master.read_bytes() == before, "the master itself was saved into"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["song.m4a"]


@needs_ffmpeg
@pytest.mark.parametrize("writer", sorted(WRITERS))
def test_tags_are_written_and_the_audio_is_untouched(tmp_path, writer):
    master = _m4a(tmp_path / "song.m4a")
    before_audio = audio_hash(master, strict=True)
    before_tags = dict(MP4(master).tags or {})
    assert WRITERS[writer](master)
    assert dict(MP4(master).tags or {}) != before_tags, "nothing was written"
    assert audio_hash(master, strict=True) == before_audio
    assert sorted(p.name for p in tmp_path.iterdir()) == ["song.m4a"]
