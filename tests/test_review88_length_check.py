"""Review of #88, finding 15 (2026-10-07). When a copy's length disagrees with
its master's header, the bake decodes the master to measure its real length.
If that decode fails, the copy is still refused -- deliberately, see
test_length_check_measures_master.py -- but the message said only "length
changed", a verdict on the copy it could not give. It now says the master could
not be measured. (The review's other claims did not hold: the 600 s is working
time, which a throttle pause does not use, and not a track length.)
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from musaeus import edition_bake

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="requires ffmpeg")


def test_an_unmeasurable_master_is_not_called_a_length_change(tmp_path, monkeypatch):
    copy = tmp_path / "copy.m4a"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=d=2",
                    "-c:a", "alac", str(copy)], check=True)  # fmt: skip
    master_info = {"format": {"duration": "1800.0", "filename": str(tmp_path / "master.m4a")},
                   "streams": [{"codec_type": "audio", "sample_rate": "44100"}]}  # fmt: skip

    def could_not_finish(path):
        return None

    monkeypatch.setattr(edition_bake, "decoded_seconds", could_not_finish)

    with pytest.raises(edition_bake.BakeError) as caught:
        edition_bake.verify(master_info, copy, float(edition_bake.TARGET_I))

    assert "could not be decoded" in str(caught.value), str(caught.value)
