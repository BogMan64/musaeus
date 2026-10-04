"""The tagger writes the track number into an MP4 file, and says so honestly.

Found 2026-10-03: the MP4 branch of _write_tags had no mapping for "track".
A track change was skipped without a word, audio.save() rewrote the file
anyway, and the stage logged TAGGER_WRITE -- so 1,983 masters whose track tag
had been lost were "written" on every Act 3, never converged, and every
edition build re-tagged their copies. Grey: "Track numbers please return".
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.mp4 import MP4

from musaeus.stages.tagger import _read_tags, _write_tags

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")


@pytest.fixture
def m4a(tmp_path) -> Path:
    p = tmp_path / "song.m4a"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=1", "-c:a", "alac", str(p)],
        check=True,
        capture_output=True,
    )
    return p


def test_a_track_number_is_written(m4a):
    assert _write_tags(m4a, {"track": "361"})
    assert MP4(str(m4a)).tags["trkn"] == [(361, 0)]
    assert _read_tags(m4a)["track"] == "361", "the read side must see what was written"


def test_an_existing_total_is_kept(m4a):
    audio = MP4(str(m4a))
    audio["trkn"] = [(5, 12)]
    audio.save()
    assert _write_tags(m4a, {"track": "7"})
    assert MP4(str(m4a)).tags["trkn"] == [(7, 12)]


def test_a_field_the_writer_cannot_store_is_a_failure_not_a_silent_success(m4a):
    before = m4a.stat().st_mtime_ns
    assert _write_tags(m4a, {"no_such_field": "x"}) is False
    assert m4a.stat().st_mtime_ns == before, "nothing stored, so nothing rewritten"


def test_a_second_pass_finds_nothing_to_change(m4a):
    """Convergence: after writing, the tagger's own comparison is satisfied."""
    from musaeus.stages.tagger import TaggerStage

    _write_tags(m4a, {"track": "4"})
    row = {
        "file_path": str(m4a),
        "track": 4,
        "album": None,
        "title": None,
        "genre": None,
        "year": None,
        "artist": None,
    }
    assert "track" not in TaggerStage()._compute_changes(row, _read_tags(m4a))
