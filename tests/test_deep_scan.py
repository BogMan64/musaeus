"""The deep integrity scan: does every master still decode?

Two masters were found truncated 2026-09-01 -- an intact container header
over missing audio, so a 5-minute song decoding to 55 seconds. They
surfaced only because the Car encoder verifies output duration against the
source and refused to ship the short version. Nothing in the pipeline was
looking for them, and `ffmpeg_decode_check(path, seconds=0)` had been
ported from ORPHEUS the day before and was never called from anywhere.

The design decision worth protecting is that **size is a prioritiser, not
a verdict.** Bytes-per-second flagged 418 files; 93 were lossless and only
2 were actually damaged. The other 91 were Bing Crosby, Count Basie,
Billie Holiday -- old mono recordings that legitimately compress to ~13%
of PCM. Acting on the ratio alone would have sent 91 undamaged masters for
re-sourcing. So the ratio only decides what to decode FIRST.

That ordering is not cosmetic. A scan that yields to the user constantly
may take days of wall clock, so it must spend its first minutes on the
files most likely to be damaged. Verified against the live library: both
known-corrupt masters were found within the first 3 files checked.
"""

from __future__ import annotations

import sqlite3

import pytest

from musaeus.deep_scan import (
    SUSPICIOUS_RATIO, ensure_columns, looks_suspicious, pcm_bytes_per_second,
    pending_rows, size_ratio,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE archive (
        file_path TEXT PRIMARY KEY, artist TEXT, title TEXT, status TEXT,
        duration REAL, size_bytes INTEGER, sample_rate INTEGER, codec TEXT)""")
    return c


def _add(c, path, **kw):
    d = dict(artist="A", title="T", status="CATALOGUED", duration=300.0,
             size_bytes=100_000_000, sample_rate=44100, codec="alac")
    d.update(kw)
    c.execute("INSERT INTO archive (file_path,artist,title,status,duration,"
              "size_bytes,sample_rate,codec) VALUES (?,?,?,?,?,?,?,?)",
              (path, d["artist"], d["title"], d["status"], d["duration"],
               d["size_bytes"], d["sample_rate"], d["codec"]))


class TestSizeRatio:
    def test_the_real_truncated_file_scores_far_below_threshold(self) -> None:
        """BTO — Takin' Care of Business: 13.9 MB claiming 290.8s at 192 kHz."""
        r = size_ratio(13_876_300, 290.8, 192_000)
        assert r < 0.07
        assert r < SUSPICIOUS_RATIO

    def test_a_normal_lossless_file_is_not_flagged(self) -> None:
        assert size_ratio(150_000_000, 300.0, 44100) > SUSPICIOUS_RATIO

    def test_unknowable_inputs_return_none_rather_than_a_guess(self) -> None:
        assert size_ratio(None, 300.0, 44100) is None
        assert size_ratio(1000, 0, 44100) is None
        assert size_ratio(1000, None, 44100) is None

    def test_missing_sample_rate_assumes_cd(self) -> None:
        assert pcm_bytes_per_second(None) == pcm_bytes_per_second(44100)


class TestSuspicionIsLosslessOnly:
    def test_a_small_lossy_file_is_never_suspicious(self, conn) -> None:
        """AAC is SUPPOSED to be small -- the ratio says nothing about it.
        325 of the original 418 flagged files were lossy."""
        _add(conn, "/a.m4a", codec="aac", size_bytes=6_000_000)
        row = conn.execute("SELECT * FROM archive").fetchone()
        assert looks_suspicious(row) is False

    def test_a_small_lossless_file_is_suspicious(self, conn) -> None:
        _add(conn, "/b.m4a", codec="alac", size_bytes=6_000_000)
        row = conn.execute("SELECT * FROM archive").fetchone()
        assert looks_suspicious(row) is True

    def test_a_healthy_lossless_file_is_not(self, conn) -> None:
        _add(conn, "/c.m4a", codec="alac", size_bytes=150_000_000)
        row = conn.execute("SELECT * FROM archive").fetchone()
        assert looks_suspicious(row) is False


class TestOrdering:
    def test_suspicious_files_are_checked_first(self, conn) -> None:
        """A scan interrupted after five minutes should have spent them on
        the likeliest damage, not on whatever sorts first."""
        ensure_columns(conn)
        _add(conn, "/healthy1.m4a", size_bytes=150_000_000)
        _add(conn, "/suspect.m4a", size_bytes=6_000_000)
        _add(conn, "/healthy2.m4a", size_bytes=150_000_000)
        order = [r["file_path"] for r in pending_rows(conn)]
        assert order[0] == "/suspect.m4a"

    def test_most_suspicious_comes_before_merely_suspicious(self, conn) -> None:
        ensure_columns(conn)
        _add(conn, "/worse.m4a", size_bytes=3_000_000)
        _add(conn, "/bad.m4a", size_bytes=9_000_000)
        assert [r["file_path"] for r in pending_rows(conn)][0] == "/worse.m4a"


class TestResumability:
    def test_already_checked_rows_are_not_repeated(self, conn) -> None:
        """A scan interrupted constantly must converge, not restart."""
        ensure_columns(conn)
        _add(conn, "/done.m4a")
        _add(conn, "/todo.m4a")
        conn.execute("UPDATE archive SET decode_checked_at='2026-09-01' "
                     "WHERE file_path='/done.m4a'")
        assert [r["file_path"] for r in pending_rows(conn)] == ["/todo.m4a"]

    def test_only_catalogued_rows_are_scanned(self, conn) -> None:
        ensure_columns(conn)
        _add(conn, "/keep.m4a", status="CATALOGUED")
        _add(conn, "/skip.m4a", status="QUARANTINED")
        assert [r["file_path"] for r in pending_rows(conn)] == ["/keep.m4a"]

    def test_columns_are_added_idempotently(self, conn) -> None:
        ensure_columns(conn)
        ensure_columns(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(archive)")}
        assert {"decode_checked_at", "decode_ok", "decode_errors"} <= cols


class TestIdleOnly:
    """Idle-ONLY is the inverse of the throttle, and the difference matters.

    The throttle pauses work when the machine is busy and runs at full
    speed when it cannot measure idle -- a build has a deadline. This has
    none: a full decode of 10,446 masters is hours nobody is waiting on, so
    it must not exist while Grey is at the keyboard, and must stay stopped
    where idle is unmeasurable rather than assume it is safe to run.
    """

    def test_unmeasurable_idle_means_not_idle(self, monkeypatch) -> None:
        """Headless, cron, Wayland without the extension: stay stopped.
        The throttle makes the opposite choice on purpose."""
        import musaeus.idle_throttle as it

        class _Blind:
            available = False

        monkeypatch.setattr(it, "_XIdle", lambda: _Blind())
        assert it.is_idle() is False
        assert it.idle_seconds() is None

    def test_idle_threshold_is_respected(self, monkeypatch) -> None:
        import musaeus.idle_throttle as it

        class _Fake:
            available = True
            def __init__(self, ms): self._ms = ms
            def idle_ms(self): return self._ms

        monkeypatch.setattr(it, "_XIdle", lambda: _Fake(10_000))
        assert it.is_idle(threshold_s=40) is False
        monkeypatch.setattr(it, "_XIdle", lambda: _Fake(60_000))
        assert it.is_idle(threshold_s=40) is True

    def test_a_probe_that_raises_is_treated_as_busy(self, monkeypatch) -> None:
        """Failing safe means not running, not running anyway."""
        import musaeus.idle_throttle as it

        class _Broken:
            available = True
            def idle_ms(self): raise OSError("display went away")

        monkeypatch.setattr(it, "_XIdle", lambda: _Broken())
        assert it.is_idle() is False
