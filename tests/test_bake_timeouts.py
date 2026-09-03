"""The bake's timeout policy was exactly inverted, and it mattered.

The two calls that legitimately take minutes -- the loudnorm measurement
pass and the bake itself, both full decodes of the whole track -- had NO
timeout, so a hung ffmpeg stalled the run for ever. The one call that
should take milliseconds, ffprobe reading stream metadata, was the only
one with a deadline, and at a flat 30 s.

Under a saturated disk that is the one that fires. Measured 2026-09-01
against a concurrent car encode: these very tests failed on a 30 s
ffprobe while spending 2.7 s of CPU across 62 s of wall clock -- waiting,
not computing. Nothing was wrong with the files.

And subprocess.TimeoutExpired inherits from SubprocessError, not from
CalledProcessError, RuntimeError or OSError -- the three things
_process_one catches. So a timeout escaped every handler and propagated,
turning one stalled file into a dead run. On the 486 GB first bake, which
has never been run at scale, that is the difference between one ERROR
line and starting over with no idea which file did it.
"""

from __future__ import annotations

import importlib.util as ilu
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "alac_library" / "build_alac_library.py"
_spec = ilu.spec_from_file_location("build_alac_library_timeouts", _SCRIPT)
assert _spec and _spec.loader
bal = ilu.module_from_spec(_spec)
sys.modules["build_alac_library_timeouts"] = bal
_spec.loader.exec_module(bal)


def _probe(duration=None, rate=None):
    p: dict = {"format": {}, "streams": []}
    if duration is not None:
        p["format"]["duration"] = duration
    if rate is not None:
        p["streams"].append({"codec_type": "audio", "sample_rate": rate})
    return p


# ── The deadline scales with the work ─────────────────────────────────────────


def test_a_longer_track_gets_a_longer_deadline() -> None:
    short = bal._ffmpeg_timeout(_probe("180", 48000))
    long_ = bal._ffmpeg_timeout(_probe("3600", 48000))
    assert long_ > short


def test_a_hi_res_track_gets_a_longer_deadline_than_the_same_length_at_48k() -> None:
    """192 kHz decodes four times the samples. A flat guess is too tight
    here and too loose everywhere else -- the same reasoning hasher.py
    records for _audio_hash_timeout."""
    assert bal._ffmpeg_timeout(_probe("600", 192000)) > bal._ffmpeg_timeout(_probe("600", 48000))


def test_an_hour_long_track_is_given_at_least_its_own_length() -> None:
    """These are hang detectors, not performance budgets. The passes run
    at roughly 100x realtime, so a deadline near 1x realtime is only ever
    reached by a genuine stall."""
    assert bal._ffmpeg_timeout(_probe("3600", 48000)) >= 3600


def test_a_short_track_still_gets_a_generous_floor() -> None:
    """2% of nothing is nothing. Without a floor a 3-second file would be
    killed the instant the disk got busy."""
    assert bal._ffmpeg_timeout(_probe("3", 48000)) >= 300


# ── ...and never crashes on a probe it cannot read ────────────────────────────


@pytest.mark.parametrize(
    "probe",
    [
        {},
        {"format": {}, "streams": []},
        {"format": {"duration": None}, "streams": [{"codec_type": "audio", "sample_rate": None}]},
        {"format": {"duration": "not-a-number"}, "streams": []},
        {"format": {"duration": "60"}, "streams": [{"codec_type": "audio", "sample_rate": "x"}]},
        {"streams": [{"codec_type": "video"}]},
    ],
)
def test_an_unreadable_probe_falls_back_to_the_floor(probe) -> None:
    """A deadline helper that raises would be worse than the bug it fixes."""
    assert bal._ffmpeg_timeout(probe) >= 300


# ── The fast call is no longer the only one with a deadline ───────────────────


def test_probe_deadline_is_generous_not_thirty_seconds() -> None:
    """30 s was tight enough that ordinary disk contention hit it. ffprobe
    reads metadata; anything near this is contention, never the file."""
    assert bal._PROBE_TIMEOUT >= 120


def test_both_ffprobe_calls_use_the_same_deadline(tmp_path: Path) -> None:
    """_probe_streams had a flat 30 s and source_sample_fmt had none. Two
    reads of the same kind, disagreeing, is how one of them ends up wrong.

    Asserted through the calls rather than by grepping the source, because
    the source spelling is not the behaviour."""
    seen = []

    def _spy(cmd, deadline, check=False):
        seen.append(deadline)
        return subprocess.CompletedProcess(cmd, 0, '{"format":{},"streams":[]}', "")

    f = tmp_path / "t.m4a"
    f.write_bytes(b"x")
    with patch.object(bal, "_run_with_deadline", _spy):
        bal._probe_streams(f)
        bal.source_sample_fmt(f)

    assert seen == [bal._PROBE_TIMEOUT, bal._PROBE_TIMEOUT]
    assert all(d >= 120 for d in seen), "30s was tight enough to fire on contention"


# ── A stall is one error line, not the end of the run ─────────────────────────


def test_a_timeout_is_reported_per_file_and_does_not_propagate(tmp_path: Path) -> None:
    """This is the whole point. Before the handler, TimeoutExpired escaped
    CalledProcessError/RuntimeError/OSError and killed the run."""
    archive = tmp_path / "ALAC_Archive"
    (archive / "A" / "B").mkdir(parents=True)
    source = archive / "A" / "B" / "t.m4a"
    source.write_bytes(b"x")
    row = {"id": 1, "file_path": str(source)}

    class _Conn:
        def execute(self, *a, **k):
            class _C:
                def fetchone(_s):
                    return {"status": "CATALOGUED", "lufs_baked_at": None}
            return _C()

    with patch.object(bal, "_probe_streams", return_value=_probe("300", 48000)), \
         patch.object(bal, "_has_attached_picture", return_value=False), \
         patch.object(
             bal, "ffmpeg_measure_loudnorm",
             side_effect=subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=600)):
        out = bal._process_one(_Conn(), row, archive, tmp_path / "Lib", execute=True)

    assert isinstance(out, str), "a stalled file must not raise out of _process_one"
    assert out.startswith("ERROR"), out
    assert "600s" in out and "stall" in out, out


# ── The deadline must not count time the throttle forbade work ────────────────
#
# This is the part that made the whole diagnosis wrong for an hour. The
# idle throttle SIGSTOPs ffmpeg whenever someone is at the keyboard, so
# wall clock includes time the child was FORBIDDEN to run. A flat
# wall-clock deadline kills exactly the work the throttle is protecting.
#
# Measured 2026-09-01: the bake fixture takes 0.626 s with the throttle
# off and sits past 90 s with it on. Nothing was slow; ffprobe was stopped.


class _FakeThrottle:
    """Reports pausing for `rate` seconds per second of wall clock."""

    def __init__(self, rate: float) -> None:
        self._start = __import__("time").monotonic()
        self._rate = rate

    @property
    def paused_seconds(self) -> float:
        import time as _t

        return (_t.monotonic() - self._start) * self._rate


def test_a_fully_paused_child_is_never_killed(monkeypatch) -> None:
    """Throttled the whole time: working time is ~0, so a 1s deadline must
    not fire on a 3s sleep. Before this, the throttle and the timeout
    fought each other and the timeout won."""
    monkeypatch.setattr(bal, "_ACTIVE_THROTTLE", _FakeThrottle(rate=1.0))
    out = bal._run_with_deadline(["sleep", "3"], deadline=1)
    assert out.returncode == 0


def test_a_genuinely_hung_child_is_still_killed(monkeypatch) -> None:
    """Nothing paused it, so elapsed time IS working time. The deadline
    must still catch a real stall -- that is what it is for."""
    monkeypatch.setattr(bal, "_ACTIVE_THROTTLE", None)
    with pytest.raises(subprocess.TimeoutExpired):
        bal._run_with_deadline(["sleep", "30"], deadline=1)


def test_output_and_returncode_survive_the_wrapper() -> None:
    out = bal._run_with_deadline(["sh", "-c", "echo hi; exit 0"], deadline=30)
    assert out.returncode == 0 and "hi" in out.stdout


def test_check_still_raises_calledprocesserror(monkeypatch) -> None:
    """The bake relies on check=True to turn a bad exit into the ERROR
    line _process_one already knows how to report."""
    monkeypatch.setattr(bal, "_ACTIVE_THROTTLE", None)
    with pytest.raises(subprocess.CalledProcessError):
        bal._run_with_deadline(["sh", "-c", "exit 3"], deadline=30, check=True)


def test_the_throttle_reports_the_time_it_held_children_stopped() -> None:
    """_run_with_deadline can only discount paused time if the throttle
    admits to it. Only the throttle knows the difference between 'stopped'
    and 'stuck'."""
    import time as _t

    from musaeus.idle_throttle import IdleThrottle

    t = IdleThrottle()
    assert t.paused_seconds == 0.0
    t._mark_paused(True)
    _t.sleep(0.05)
    assert t.paused_seconds >= 0.05
    t._mark_paused(False)
    settled = t.paused_seconds
    _t.sleep(0.05)
    assert t.paused_seconds == settled, "the counter must stop when work resumes"
