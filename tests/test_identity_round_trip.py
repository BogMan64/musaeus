"""Writing identity to a file is pointless unless a rebuild reads it back.

IdentityTagStage writes MBIDs and fingerprints so they never need
re-acquiring: an MBID costs a rate-limited second, a fingerprint ~0.8 s of
fpcalc. Across 10,000 files that is hours. But rebuild_from_disk's
_read_all_tags recovered bpm, key and loudness and ignored identity
entirely -- a round trip with only one half built, which is worth exactly
nothing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from musaeus.identity_tags import read_identity, write_identity
from musaeus.rebuild_from_disk import _read_all_tags

MBID = "4ef7a9e2-2cf5-483a-8616-ef7791a98026"
FP = "AQADtEmikYmSJFHyHT_x4zp-XPiPH9fx48J__LiOHxfx48J__A"


def _encode(path: Path, seconds: int = 3) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "alac", str(path)],
        capture_output=True,
    )
    return r.returncode == 0 and path.exists()


@pytest.fixture
def track(tmp_path) -> Path:
    p = tmp_path / "t.m4a"
    if not _encode(p):
        pytest.skip("ffmpeg unavailable")
    return p


class TestIdentitySurvivesToTheRebuild:
    def test_mbids_written_to_the_file_are_recovered(self, track):
        write_identity(track, {"mb_artist_id": MBID,
                               "acousticid_recording": "rec-123"})
        got = _read_all_tags(track)
        assert got.get("mb_artist_id") == MBID
        assert got.get("acousticid_recording") == "rec-123"

    def test_a_fingerprint_matching_the_audio_is_trusted(self, track):
        # 3 s of audio, and the tag agrees.
        write_identity(track, {"chromaprint": FP, "chromaprint_duration": "3"})
        got = _read_all_tags(track)
        assert got.get("chromaprint") == FP

    def test_a_fingerprint_that_outlived_its_audio_is_refused(self, track):
        # The tag claims 300 s; the file is 3 s. The fingerprint describes
        # PCM that is no longer there, so it must not be trusted.
        write_identity(track, {"chromaprint": FP, "chromaprint_duration": "300"})
        got = _read_all_tags(track)
        assert "chromaprint" not in got, (
            "a fingerprint whose recorded duration disagrees with the audio "
            "must not be recovered"
        )

    def test_a_file_with_no_identity_yields_nothing(self, track):
        got = _read_all_tags(track)
        for col in ("mb_artist_id", "acousticid_recording", "chromaprint"):
            assert col not in got

    def test_the_duration_field_itself_round_trips(self, track):
        write_identity(track, {"chromaprint_duration": "3"})
        assert read_identity(track).get("chromaprint_duration") == "3"


class TestUncheckableFingerprint:
    """A fingerprint that cannot be checked must not be trusted.

    The first guard read `if recorded and actual and mismatch: distrust /
    else: trust`, so it trusted whenever there was nothing to compare --
    and a file tagged before chromaprint_duration joined IDENTITY_FIELDS
    carries exactly that: a fingerprint and no duration. Same
    check-that-cannot-fire shape as the bug the guard replaced.
    """

    def test_a_fingerprint_without_a_recorded_duration_is_refused(self, track):
        write_identity(track, {"chromaprint": FP})
        assert read_identity(track).get("chromaprint") == FP, (
            "precondition: the tag is on the file; only the duration is absent"
        )

        got = _read_all_tags(track)

        assert "chromaprint" not in got, (
            "a fingerprint with nothing to check it against was trusted"
        )

    def test_probing_is_skipped_when_the_duration_is_already_known(self, track, monkeypatch):
        """scan_and_rebuild has already probed this file before calling us."""
        import musaeus.rebuild_from_disk as rb

        calls = {"n": 0}
        real = rb._probe

        def _counting(p):
            calls["n"] += 1
            return real(p)

        monkeypatch.setattr(rb, "_probe", _counting)
        write_identity(track, {"chromaprint": FP, "chromaprint_duration": "3"})

        got = _read_all_tags(track, known_duration=3.0)

        assert got.get("chromaprint") == FP, "the fingerprint should still be trusted"
        assert calls["n"] == 0, (
            "re-probed a file whose duration the caller already knew; "
            "~50-100ms per file across a 10,000-file rebuild"
        )

    def test_it_still_probes_when_the_caller_knows_nothing(self, track, monkeypatch):
        """The fallback must survive: a caller with no duration to offer
        still gets the fingerprint checked rather than trusted blindly."""
        import musaeus.rebuild_from_disk as rb

        calls = {"n": 0}
        real = rb._probe

        def _counting(p):
            calls["n"] += 1
            return real(p)

        monkeypatch.setattr(rb, "_probe", _counting)
        write_identity(track, {"chromaprint": FP, "chromaprint_duration": "3"})

        got = _read_all_tags(track)

        assert calls["n"] == 1
        assert got.get("chromaprint") == FP
