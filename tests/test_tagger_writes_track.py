"""The tagger leaves the track number alone, and says so honestly.

The story in two rulings:

- 2026-10-03, Grey: "Track numbers please return". The MP4 writer had no
  mapping for "track", so a track change was skipped without a word,
  audio.save() rewrote the file anyway, and the stage logged TAGGER_WRITE --
  1,983 masters whose track tag had been lost were "written" on every Act 3,
  never converging.
- 2026-11-27, Grey: "Strip them. and have tagger stop writting them back".
  That reverses the first: 1,402 catalogue rows carry a track number, the
  tagger put it into the file on every pass, and any strip was undone by the
  next run.

So the tagger no longer proposes a track change, and a caller that asks it to
write one is told, rather than silently served. Removing a number that is
already in a file is `scripts/strip_track.py`'s job.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.mp4 import MP4

from musaeus.stages.tagger import TaggerStage, _read_tags, _write_tags

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


def test_a_catalogue_track_number_is_not_written(m4a):
    """The row says 361; the file gets nothing."""
    db_row = {"artist": "Toto", "title": "Rosanna", "album": "Toto IV", "track": "361"}
    file_tags = _read_tags(m4a)

    changes = TaggerStage()._compute_changes(db_row, file_tags)

    assert "track" not in changes, "the tagger proposed the track number it must not write"


def test_the_writer_refuses_a_track_change_rather_than_skipping_it(m4a):
    """A caller that insists gets a failure, not a silent skip with success
    reported -- the shape of the 2026-10-03 bug, inverted."""
    assert _write_tags(m4a, {"track": "361"}) is False
    assert "trkn" not in (MP4(str(m4a)).tags or {})


def test_an_existing_track_number_is_left_alone(m4a):
    """Stripping is a separate, explicit act. The tagger must not remove one
    while it is normalising something else."""
    audio = MP4(str(m4a))
    audio.tags["trkn"] = [(3, 10)]
    audio.save()

    assert _write_tags(m4a, {"album": "Toto IV"}) is True
    assert MP4(str(m4a)).tags["trkn"] == [(3, 10)], "the tagger removed a track number"


def test_a_second_pass_finds_nothing_to_change(m4a):
    """The track row stays different from the file for ever, and that must
    not make the stage churn."""
    db_row = {"artist": "Toto", "title": "Rosanna", "album": "Toto IV", "track": "361"}

    assert _write_tags(m4a, TaggerStage()._compute_changes(db_row, _read_tags(m4a)))
    second = TaggerStage()._compute_changes(db_row, _read_tags(m4a))

    assert second == {}, f"the stage still wants to write {second} on every pass"
