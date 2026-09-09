"""The last check before a lossless original is deleted must fail closed.

P0-A, reported 2026-09-09.

`_verify_conversion` is the only thing standing between a bad encode and
`source.unlink()`. It read:

    if (src_dur is not None and out_dur is not None
            and abs(src_dur - out_dur) > _DURATION_TOLERANCE_SEC):
        raise CanonicalizeError(...)

If **either** duration was `None` the comparison never ran and the function
returned success. And `_duration()` read only `format.duration`, with no
stream-level fallback — while WAV and AIFF routinely carry no container
duration at all, and those are exactly the sources this stage converts and
then deletes.

So for such a source, verification degraded to *"the output has an audio
stream"*. A two-second truncated ALAC passed, and the FLAC original was
unlinked.

**Why it survived two and a half weeks of intensive repair work:** the
function sits in the uncovered range. `canonicalize.py` held 20% coverage
while the repository carried 2,500 passing tests, because CI never installed
ffmpeg and every test that would have exercised this path skipped. The one
class of bug that cannot be caught is the one whose tests never run — which
is why the ffmpeg CI change landed in the same commit as this file.

These tests deliberately avoid ffmpeg. They drive `_verify_conversion`
through fabricated ffprobe output, so they run everywhere — including the CI
that could not previously see this function at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.stages.canonicalize import CanonicalizeError, _verify_conversion


def _probe(duration=None, *, streams=1, stream_duration=None):
    """A minimal ffprobe payload."""
    out = {"streams": [], "format": {}}
    for _ in range(streams):
        s = {"codec_type": "audio"}
        if stream_duration is not None:
            s["duration"] = stream_duration
        out["streams"].append(s)
    if duration is not None:
        out["format"]["duration"] = duration
    return out


@pytest.fixture
def probes(monkeypatch):
    """Drive _verify_conversion without touching ffprobe or the disk."""
    table: dict[str, dict] = {}

    def fake(path: Path) -> dict:
        return table[path.name]

    monkeypatch.setattr("musaeus.stages.canonicalize._probe_streams", fake)
    return table


# ── the finding ──────────────────────────────────────────────────────────────


def test_an_unmeasurable_source_duration_refuses(probes) -> None:
    """The WAV/AIFF case. Before the fix this returned success."""
    probes["src.wav"] = _probe(duration=None)
    probes["out.m4a"] = _probe(duration="180.0")

    with pytest.raises(CanonicalizeError, match="could not determine"):
        _verify_conversion(Path("src.wav"), Path("out.m4a"))


def test_an_unmeasurable_output_duration_refuses(probes) -> None:
    probes["src.flac"] = _probe(duration="180.0")
    probes["out.m4a"] = _probe(duration=None)

    with pytest.raises(CanonicalizeError, match="could not determine"):
        _verify_conversion(Path("src.flac"), Path("out.m4a"))


def test_a_truncated_output_no_longer_passes_on_a_durationless_source(probes) -> None:
    """The concrete data-loss path, end to end.

    A 3-minute WAV with no container duration, converted to a 2-second
    fragment. Before the fix: no comparison, success, original deleted.
    """
    probes["master.wav"] = _probe(duration=None)
    probes["staged.m4a"] = _probe(duration="2.0")

    with pytest.raises(CanonicalizeError):
        _verify_conversion(Path("master.wav"), Path("staged.m4a"))


# ── the fallback that makes failing closed practical ─────────────────────────


def test_a_stream_level_duration_is_used_when_the_container_has_none(probes) -> None:
    """Fail-closed must not mean 'refuse every WAV'.

    ffprobe usually reports a per-stream duration even when the container
    carries none, so reading only `format.duration` threw away the answer and
    then treated its absence as permission to proceed.
    """
    probes["src.wav"] = _probe(duration=None, stream_duration="180.0")
    probes["out.m4a"] = _probe(duration="180.4")

    _verify_conversion(Path("src.wav"), Path("out.m4a"))  # must not raise


def test_a_stream_level_mismatch_is_still_caught(probes) -> None:
    probes["src.wav"] = _probe(duration=None, stream_duration="180.0")
    probes["out.m4a"] = _probe(duration="2.0")

    with pytest.raises(CanonicalizeError, match="duration mismatch"):
        _verify_conversion(Path("src.wav"), Path("out.m4a"))


# ── the check the docstring claimed and the code never made ──────────────────


def test_a_dropped_audio_stream_is_caught(probes) -> None:
    """Two audio streams in, one out, identical duration."""
    probes["src.mka"] = _probe(duration="180.0", streams=2)
    probes["out.m4a"] = _probe(duration="180.0", streams=1)

    with pytest.raises(CanonicalizeError, match="audio stream count"):
        _verify_conversion(Path("src.mka"), Path("out.m4a"))


def test_no_audio_stream_at_all_is_still_caught(probes) -> None:
    probes["src.flac"] = _probe(duration="180.0")
    probes["out.m4a"] = _probe(duration="180.0", streams=0)

    with pytest.raises(CanonicalizeError, match="no audio stream"):
        _verify_conversion(Path("src.flac"), Path("out.m4a"))


# ── and the ordinary case must still pass ────────────────────────────────────


def test_a_good_conversion_passes(probes) -> None:
    probes["src.flac"] = _probe(duration="240.0")
    probes["out.m4a"] = _probe(duration="240.3")
    _verify_conversion(Path("src.flac"), Path("out.m4a"))


def test_drift_inside_the_ruled_tolerance_passes(probes) -> None:
    """2.0 s is the value the 2026-09-02 ruling settled on."""
    from musaeus.duration import TOLERANCE_SEC

    probes["src.flac"] = _probe(duration="240.0")
    probes["out.m4a"] = _probe(duration=str(240.0 + TOLERANCE_SEC - 0.1))
    _verify_conversion(Path("src.flac"), Path("out.m4a"))
