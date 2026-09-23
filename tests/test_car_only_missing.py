"""--only-missing builds the gap, not the library.

The 2026-09-15 build reported success leaving 726 catalogued tracks with no
car file, every one of which had a master on disk. Reaching them by encoding
the whole catalogue again is nine hours to reproduce 10,753 files that are
already correct; leaning on the resume check instead means ffprobing all of
them to discover what is already there. The catalogue already knows -- it is
one query.

The selection has to stay a SUBSET of the normal edition selection, never a
replacement for it. select_edition applies the genre budget and the edition
spec; a --only-missing that queried `archive` directly would quietly bypass
both and put tracks in the car that the budget had excluded.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "car_library"))

import build_car_library as B  # noqa: E402


class _Track:
    def __init__(self, fp: Path):
        self.file_path = fp


class _Sel:
    def __init__(self, tracks):
        self.included = tracks
        self.skipped_for_budget = []


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE archive (file_path TEXT, status TEXT, car_export_path TEXT)")
    return c


def _row(conn, fp: str, car: str = ""):
    conn.execute("INSERT INTO archive VALUES (?, 'CATALOGUED', ?)", (fp, car))
    conn.commit()


class TestItAsksTheCatalogueWhatIsMissing:
    def test_rows_with_a_car_file_are_excluded(self, conn, tmp_path, monkeypatch):
        done, gap = tmp_path / "done.m4a", tmp_path / "gap.m4a"
        for p in (done, gap):
            p.write_bytes(b"\0")
        _row(conn, str(done), car="/car/done.m4a")
        _row(conn, str(gap))

        seen = {}

        def fake_select(c, spec, budget_bytes=None):
            return _Sel([_Track(done), _Track(gap)])

        def fake_master(fp, lib, arc):
            from musaeus.editions import MasterResolution

            return MasterResolution(Path(fp), True)

        def fake_out(t, spec, staging):
            return staging / Path(t.file_path).name

        import musaeus.editions as E

        monkeypatch.setattr(E, "select_edition", fake_select)
        monkeypatch.setattr(E, "master_path_for", fake_master)
        monkeypatch.setattr(E, "output_path_for", fake_out)
        monkeypatch.setattr(
            B,
            "get_config",
            lambda: type("C", (), {"alac_library": tmp_path, "alac_archive": tmp_path})(),
        )

        staging = tmp_path / "stage"
        staging.mkdir()
        files, tracks, _, _ = B.stage_from_catalogue(conn, staging, only_missing=True)
        seen["n"] = len(files)
        assert seen["n"] == 1, "only the track with no car file should be staged"
        assert files[0].name == "gap.m4a"

    def test_without_the_flag_everything_is_staged(self, conn, tmp_path, monkeypatch):
        done, gap = tmp_path / "done.m4a", tmp_path / "gap.m4a"
        for p in (done, gap):
            p.write_bytes(b"\0")
        _row(conn, str(done), car="/car/done.m4a")
        _row(conn, str(gap))

        import musaeus.editions as E

        monkeypatch.setattr(
            E, "select_edition", lambda c, s, budget_bytes=None: _Sel([_Track(done), _Track(gap)])
        )
        monkeypatch.setattr(
            E,
            "master_path_for",
            lambda fp, lib, arc: __import__(
                "musaeus.editions", fromlist=["MasterResolution"]
            ).MasterResolution(Path(fp), True),
        )
        monkeypatch.setattr(E, "output_path_for", lambda t, s, st: st / Path(t.file_path).name)
        monkeypatch.setattr(
            B,
            "get_config",
            lambda: type("C", (), {"alac_library": tmp_path, "alac_archive": tmp_path})(),
        )

        staging = tmp_path / "stage2"
        staging.mkdir()
        files, _, _, _ = B.stage_from_catalogue(conn, staging, only_missing=False)
        assert len(files) == 2


class TestItStaysASubsetOfTheEditionSelection:
    def test_the_filter_is_applied_after_select_edition(self):
        """A --only-missing that queried `archive` directly would bypass the
        genre budget and the edition spec, and put tracks in the car that the
        budget had deliberately excluded."""
        src = (
            Path(__file__).resolve().parents[1] / "scripts" / "car_library" / "build_car_library.py"
        ).read_text()
        sel = src.index("sel = select_edition(")
        filt = src.index("if only_missing:")
        assert sel < filt, "the gap filter must narrow select_edition's result"

    def test_the_flag_reaches_the_stager(self):
        src = (
            Path(__file__).resolve().parents[1] / "scripts" / "car_library" / "build_car_library.py"
        ).read_text()
        assert "only_missing=args.only_missing" in src
        assert '"--only-missing"' in src
