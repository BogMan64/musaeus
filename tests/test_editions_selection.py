"""Edition selection -- deciding WHAT goes in, before anything is encoded.

Building an edition is two jobs, and only the encoding half existed:
build_car_library.py converts whatever is hand-dropped into a folder and
never queries the library, which is why no edition had ever been built
from the catalogue.

Two behaviours here exist specifically because ORPHEUS's
build_aac_port_iphone.py got them wrong:

  - genre matching is EXACT. ORPHEUS matched by substring, so selecting
    "Rock" also pulled in "Classic Rock", "Hard Rock" and "Punk Rock" --
    four genres in an edition asked to hold one.
  - a track too big for the remaining budget is SKIPPED, not fatal. A
    naive fill stops at the first oversized item and strands the entire
    tail of the priority order behind one long recording.

Size is estimated, never measured, because a 32 GB device budget has to be
checked before a six-hour encode rather than after it.
"""

from __future__ import annotations

import sqlite3

import pytest

from musaeus.editions import (
    CAR, IPHONE, LOSSLESS, DEFAULT_GENRE_PRIORITY, EditionSpec, Track,
    estimated_bytes, genre_rank, load_tracks, output_path_for, select_edition,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE archive (
        file_path TEXT PRIMARY KEY, artist TEXT, album TEXT, title TEXT,
        genre TEXT, duration REAL, size_bytes INTEGER, status TEXT)""")
    return c


def _add(c, path, *, artist="A", album="Al", title="T", genre="Rock",
         duration=240.0, size=40_000_000, status="CATALOGUED"):
    c.execute("INSERT INTO archive VALUES (?,?,?,?,?,?,?,?)",
              (path, artist, album, title, genre, duration, size, status))


def _track(**kw) -> Track:
    base = dict(file_path="/m/A/Al/t.m4a", artist="A", album="Al", title="T",
                genre="Rock", duration=240.0, size_bytes=40_000_000)
    base.update(kw)
    return Track(**base)


class TestSizeEstimation:
    def test_lossy_estimate_comes_from_duration_and_bitrate(self) -> None:
        """4 minutes at 256 kbps is ~7.7 MB, whatever the master weighs."""
        t = _track(duration=240.0, size_bytes=40_000_000)
        got = estimated_bytes(t, CAR)
        assert 7_500_000 < got < 8_200_000
        assert got < t.size_bytes / 4, "a lossy edition must be far smaller"

    def test_lossless_estimate_is_the_master_size(self) -> None:
        """The bake re-encodes at the same rate and depth."""
        t = _track(size_bytes=40_000_000)
        assert estimated_bytes(t, LOSSLESS) == 40_000_000

    def test_estimate_errs_high_not_low(self) -> None:
        """Overshooting a device after a long encode is the worse failure."""
        t = _track(duration=240.0)
        exact = 240.0 * 256 * 1000 / 8
        assert estimated_bytes(t, CAR) > exact

    def test_missing_duration_falls_back_to_master_size(self) -> None:
        """A budget that refuses an unmeasurable track beats one that overruns."""
        assert estimated_bytes(_track(duration=0.0), CAR) == 40_000_000


class TestGenreMatchingIsExact:
    def test_selecting_one_genre_does_not_pull_in_its_neighbours(self, conn) -> None:
        """ORPHEUS's substring match turned 'Rock' into four genres."""
        _add(conn, "/m/a.m4a", genre="Rock")
        _add(conn, "/m/b.m4a", genre="Hard Rock")
        _add(conn, "/m/c.m4a", genre="Classic Pop")
        got = {t.genre for t in load_tracks(conn, genres={"Rock"})}
        assert got == {"Rock"}

    def test_unlisted_genre_sorts_after_listed_ones_not_dropped(self) -> None:
        assert genre_rank("Rock", DEFAULT_GENRE_PRIORITY)[0] == 0
        assert genre_rank("Zydeco", DEFAULT_GENRE_PRIORITY)[0] == len(DEFAULT_GENRE_PRIORITY)

    def test_untagged_tracks_sort_last_but_still_appear(self, conn) -> None:
        _add(conn, "/m/untagged.m4a", genre="")
        _add(conn, "/m/rock.m4a", genre="Rock")
        sel = select_edition(conn, CAR)
        assert len(sel.included) == 2
        assert sel.included[-1].file_path == "/m/untagged.m4a"


class TestBudget:
    def test_no_budget_takes_everything(self, conn) -> None:
        for i in range(5):
            _add(conn, f"/m/{i}.m4a")
        assert len(select_edition(conn, CAR).included) == 5

    def test_fills_up_to_the_budget_and_reports_the_rest(self, conn) -> None:
        for i in range(10):
            _add(conn, f"/m/{i}.m4a", duration=240.0)   # ~7.8 MB each
        sel = select_edition(conn, CAR, budget_bytes=25_000_000)
        assert 0 < len(sel.included) < 10
        assert sel.estimated_bytes <= 25_000_000
        assert len(sel.included) + len(sel.skipped_for_budget) == 10

    def test_an_oversized_track_is_skipped_not_fatal(self, conn) -> None:
        """The bug this test exists for: a naive fill stops at the first
        item that will not fit and strands everything after it."""
        _add(conn, "/m/huge.m4a", genre="Rock", duration=36_000.0)     # 10 h
        _add(conn, "/m/small.m4a", genre="Rock", duration=180.0)       # 3 min
        sel = select_edition(conn, CAR, budget_bytes=20_000_000)
        assert [t.file_path for t in sel.included] == ["/m/small.m4a"]
        assert [t.file_path for t in sel.skipped_for_budget] == ["/m/huge.m4a"]

    def test_budget_fills_in_priority_order(self, conn) -> None:
        """With room for one, the higher-priority genre wins."""
        _add(conn, "/m/jazz.m4a", genre="Jazz", duration=240.0)
        _add(conn, "/m/rock.m4a", genre="Rock", duration=240.0)
        sel = select_edition(conn, CAR, budget_bytes=8_200_000)
        assert len(sel.included) == 1
        assert sel.included[0].genre == "Rock"


class TestOnlyAcceptedMusic:
    @pytest.mark.parametrize("status", ["QUARANTINED", "DUPE_REVIEW", "GHOST", "PENDING"])
    def test_non_catalogued_rows_never_reach_an_edition(self, conn, status) -> None:
        """Shipping a quarantined row would send a judgement back out as music."""
        _add(conn, "/m/ok.m4a", status="CATALOGUED")
        _add(conn, "/m/no.m4a", status=status)
        assert [t.file_path for t in load_tracks(conn)] == ["/m/ok.m4a"]


class TestDeterminism:
    def test_same_catalogue_gives_the_same_edition(self, conn) -> None:
        """A rebuild that silently differs from what was delivered is worse
        than one that fails."""
        for i in range(20):
            _add(conn, f"/m/{i}.m4a", genre=["Rock", "Jazz", "Blues"][i % 3],
                 artist=f"Artist{i % 4}", duration=200.0 + i)
        a = [t.file_path for t in select_edition(conn, CAR, budget_bytes=40_000_000).included]
        b = [t.file_path for t in select_edition(conn, CAR, budget_bytes=40_000_000).included]
        assert a == b and a


class TestOutputPaths:
    def test_mirrors_the_master_artist_album_shape(self) -> None:
        from pathlib import Path
        t = _track(file_path="/vault/ALAC_Archive/2026-08-27A/ABBA/Voulez-Vous/ABBA - Chiquitita.m4a")
        got = output_path_for(t, CAR, Path("/out"))
        assert got == Path("/out/ABBA/Voulez-Vous/ABBA - Chiquitita.m4a")


class TestSpecs:
    def test_car_and_iphone_are_lossy_and_capped_for_head_units(self) -> None:
        for spec in (CAR, IPHONE):
            assert spec.is_lossless is False
            assert spec.bitrate_kbps == 256
            assert spec.max_sample_rate == 48_000
            assert spec.lufs_target == -14.0

    def test_lossless_is_not_rate_capped(self) -> None:
        assert LOSSLESS.is_lossless is True
        assert LOSSLESS.max_sample_rate is None
        assert LOSSLESS.lufs_target == -18.0
