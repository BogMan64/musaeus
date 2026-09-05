"""A conversion that truncates must not pass verification.

CanonicalizeStage already verified three things: audio_hash survived, the
file is on disk, and the codec is really ALAC. All three pass for a
TRUNCATED conversion — right format, right identity, half the audio.

That gap stopped being hypothetical on 2026-09-01, when four masters were
found with intact container headers over missing audio: Bachman-Turner
Overdrive's "Takin' Care of Business" claimed 290.8s and decoded to 55.7s.
They were found only because the Car encoder compares output duration
against source duration and refused to ship the short version. Nothing in
the pipeline was making that comparison, and Canonicalize was about to
transcode ~19,000 FLAC files.

Two of those four were ALSO invisible to a size heuristic — Billy Joel at
29.1% of PCM and Billy Ocean at 36.2% look entirely normal. Duration is
the check that catches them; size is not.

The duration is read off the AUDIO STREAM, not the container. A truncated
file frequently keeps a plausible format-level duration, which is exactly
what made those four invisible to everything that read metadata.
"""

from __future__ import annotations

import inspect

import pytest

from musaeus.duration import tolerance_for
from musaeus.stages import canonicalize as canonicalize_mod


def _problem_for(recorded: float, actual: float) -> bool:
    """The tolerance rule as implemented: 2s floor, else 2%.

    Calls duration.tolerance_for() rather than restating the arithmetic.
    This test previously carried its own `max(2.0, recorded * 0.02)`, which
    made it a copy of exactly the constant P2-3 consolidated -- and one that
    would keep passing while production drifted away from it, since both
    sides of the comparison were the same literal.
    """
    return abs(actual - recorded) > tolerance_for(recorded)


class TestTolerance:
    def test_the_real_bto_truncation_is_caught(self) -> None:
        """290.8s recorded, 55.7s actual — the file that started this."""
        assert _problem_for(290.8, 55.74) is True

    def test_the_mcferrin_truncation_is_caught(self) -> None:
        assert _problem_for(532.63, 261.06) is True

    def test_the_two_the_size_heuristic_missed_are_caught(self) -> None:
        """Billy Joel and Billy Ocean look normal by size and are not."""
        assert _problem_for(334.19, 151.98) is True
        assert _problem_for(363.29, 145.24) is True

    @pytest.mark.parametrize("recorded,actual", [
        (300.0, 300.0),      # identical
        (300.0, 301.5),      # frame rounding
        (300.0, 298.5),
        (30.0, 31.0),        # short track, inside the 2s floor
    ])
    def test_normal_encoder_drift_is_tolerated(self, recorded, actual) -> None:
        """Verification that cries wolf on rounding gets switched off."""
        assert _problem_for(recorded, actual) is False

    def test_a_short_track_uses_the_two_second_floor_not_two_percent(self) -> None:
        """2% of 30s is 0.6s, which ordinary rounding would exceed."""
        assert _problem_for(30.0, 30.5) is False
        assert _problem_for(30.0, 15.0) is True

    def test_half_length_is_always_a_problem_at_any_duration(self) -> None:
        for d in (30.0, 120.0, 300.0, 600.0):
            assert _problem_for(d, d / 2) is True


class TestImplementationShape:
    def test_duration_is_read_from_the_audio_stream_not_the_container(self) -> None:
        """The container's duration is frequently intact on a truncated
        file — that is precisely why those four masters were invisible."""
        from pathlib import Path
        src = inspect.getsource(canonicalize_mod)
        block = src[src.index("conversion truncated the audio") - 1600:
                    src.index("conversion truncated the audio")]
        assert 'codec_type") == "audio"' in block
        assert "streams" in block

    def test_the_recorded_duration_is_selected_by_the_sample_query(self) -> None:
        from pathlib import Path
        src = inspect.getsource(canonicalize_mod)
        assert "SELECT a.file_path, a.audio_hash, a.duration" in src

    def test_a_missing_recorded_duration_does_not_raise_a_false_alarm(self) -> None:
        """Nothing to compare against is not evidence of truncation."""
        from pathlib import Path
        src = inspect.getsource(canonicalize_mod)
        assert "if recorded and recorded > 0:" in src
