"""The wanted-list membership check must not re-read the CSV per file.

P3-3, 2026-09-05. `_tunemymusic_csv_has_track` re-opened and fully
re-parsed TuneMyMusic.csv to answer one membership question, and it was
called inside sentinel's per-file hashing loop. A batch with K undecodable
files performed K full reads over a list itself growing to K rows: O(K^2)
I/O on the same disk ffmpeg is saturating, in a stage that runs an idle
throttle precisely to stay out of the way.

These tests count actual file opens, because the point is the I/O, not the
answer -- a cache that returns the right answer while still reading the
file every time would pass a correctness test and fix nothing.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from musaeus.stages import bpm


@pytest.fixture(autouse=True)
def _clear_cache():
    """Tolerant of the cache not existing, deliberately.

    The I/O test below must be runnable against a build WITHOUT the cache --
    that is what makes its red meaningful. A fixture that AttributeErrors
    there turns a behavioural failure into a collection error, which proves
    only that the fixture is coupled to the fix."""
    cache = getattr(bpm, "_TUNEMYMUSIC_CACHE", None)
    if cache is not None:
        cache.clear()
    yield
    if cache is not None:
        cache.clear()


@pytest.fixture
def counting_open(monkeypatch):
    """Count opens performed by bpm. Module globals shadow builtins, so
    putting `open` in bpm.__dict__ intercepts its calls and nothing else."""
    calls = {"n": 0}
    real = open

    def _counted(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setitem(bpm.__dict__, "open", _counted)
    return calls


def _write_csv(path: Path, rows: list[tuple[str, str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Title", "Artist", "Album"])
        for t, a in rows:
            w.writerow([t, a, ""])


class TestMembershipIsCached:
    def test_repeated_lookups_read_the_file_once(self, tmp_path, counting_open):
        csv_path = tmp_path / "TuneMyMusic.csv"
        _write_csv(csv_path, [(f"Track {i}", f"Artist {i}") for i in range(50)])

        for i in range(50):
            bpm._tunemymusic_csv_has_track(csv_path, f"Track {i}", f"Artist {i}")

        assert counting_open["n"] == 1, (
            f"50 lookups should read the CSV once, not {counting_open['n']} times"
        )

    def test_the_answers_are_still_correct(self, tmp_path):
        csv_path = tmp_path / "TuneMyMusic.csv"
        _write_csv(csv_path, [("What It Takes", "Aerosmith")])
        assert bpm._tunemymusic_csv_has_track(csv_path, "What It Takes", "Aerosmith")
        assert bpm._tunemymusic_csv_has_track(csv_path, "  what it takes ", "AEROSMITH")
        assert not bpm._tunemymusic_csv_has_track(csv_path, "The Boss", "Diana Ross")

    def test_a_missing_file_is_not_a_hit_and_is_not_cached_wrongly(self, tmp_path):
        csv_path = tmp_path / "nope.csv"
        assert not bpm._tunemymusic_csv_has_track(csv_path, "x", "y")
        _write_csv(csv_path, [("x", "y")])
        assert bpm._tunemymusic_csv_has_track(csv_path, "x", "y"), (
            "a file created after a miss must still be seen"
        )


class TestCacheStaysHonest:
    def test_an_external_edit_is_picked_up(self, tmp_path):
        """The stat guard exists so another process appending is not missed."""
        csv_path = tmp_path / "TuneMyMusic.csv"
        _write_csv(csv_path, [("One", "A")])
        assert not bpm._tunemymusic_csv_has_track(csv_path, "Two", "B")
        _write_csv(csv_path, [("One", "A"), ("Two", "B")])
        assert bpm._tunemymusic_csv_has_track(csv_path, "Two", "B"), (
            "a change on disk must invalidate the cache"
        )

    @pytest.mark.skipif(not hasattr(bpm, "remember_tunemymusic_track"),
                        reason="cache API not present in this build")
    def test_remember_keeps_the_cache_warm_without_a_reread(
        self, tmp_path, counting_open
    ):
        """An append must not force the next lookup to re-read -- that is
        exactly the O(K^2) behaviour being removed."""
        csv_path = tmp_path / "TuneMyMusic.csv"
        _write_csv(csv_path, [("One", "A")])
        bpm._tunemymusic_csv_has_track(csv_path, "One", "A")   # read #1
        before = counting_open["n"]

        with open(csv_path, "a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["Two", "B", ""])
        bpm.remember_tunemymusic_track(csv_path, "Two", "B")

        assert bpm._tunemymusic_csv_has_track(csv_path, "Two", "B")
        assert bpm._tunemymusic_csv_has_track(csv_path, "One", "A")
        assert counting_open["n"] == before, (
            "remember_tunemymusic_track should have updated the cache in place"
        )

    @pytest.mark.skipif(not hasattr(bpm, "remember_tunemymusic_track"),
                        reason="cache API not present in this build")
    def test_within_run_duplicates_are_still_caught(self, tmp_path):
        csv_path = tmp_path / "TuneMyMusic.csv"
        _write_csv(csv_path, [])
        bpm.remember_tunemymusic_track(csv_path, "Keep on Runnin", "Journey")
        assert bpm._tunemymusic_csv_has_track(csv_path, "Keep on Runnin", "Journey"), (
            "the guard against duplicate rows must work within a single run"
        )
