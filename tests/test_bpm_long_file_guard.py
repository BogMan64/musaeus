"""A 12-hour file OOM-killed a whole run at the BPM stage (2026-08-23)."""
import sqlite3
import types

from musaeus.stages.bpm import _MAX_ANALYSIS_SECONDS, _RHYTHM_MAX_SECONDS, BPMStage


def _ctx(duration, channels=2, tmp=None):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE archive (file_path TEXT, channels INT, duration REAL)")
    c.execute("INSERT INTO archive VALUES (?,?,?)", (str(tmp), channels, duration))
    ev = []
    return types.SimpleNamespace(
        conn=c, run_id="r1",
        log_event=lambda *a, **k: ev.append(k.get("event_type") or (a[0] if a else None)),
    ), ev


def test_absurdly_long_file_is_skipped_without_decoding(tmp_path):
    """The 12-hour sound bath must never reach Essentia."""
    f = tmp_path / "sound_bath.m4a"
    f.write_bytes(b"not really audio")
    ctx, ev = _ctx(720 * 60, tmp=f)
    # If the guard fails, analyze_file() runs on a junk file and raises --
    # so reaching "skip" without an exception is the assertion.
    assert BPMStage()._process_one(ctx, str(f), retag=True) == "skip"
    assert "BPM_SKIPPED_TOO_LONG" in ev


def test_real_long_music_is_still_analysed(tmp_path):
    """Whipping Post (22.9m) and Miles Davis (28.5m) must stay under the ceiling."""
    for minutes in (22.9, 28.5, 39.0):
        assert minutes * 60 < _MAX_ANALYSIS_SECONDS, f"{minutes}m would be skipped"


def test_ceiling_is_above_rhythm_window():
    """Excerpting handles the middle band; the ceiling is the last resort."""
    assert _RHYTHM_MAX_SECONDS < _MAX_ANALYSIS_SECONDS
