"""Is the car edition actually complete, or did the build only say so?

The 2026-09-15 build reported success with 726 catalogued tracks holding no
car_export_path. Every one of them had a master on disk, so every one COULD
have been encoded. Nothing anywhere said otherwise -- there was no answer to
"is the car edition complete?" short of counting by hand, and the failure
mode is the expensive quiet kind: the USB gets made, the tracks are not on
it, and the first report is somebody noticing in the car months later.

The distinction that makes this check worth having rather than noise:

    no car file, master present  -> a build could fix it. WARN.
    no car file, master gone     -> nothing can be built from a missing
                                    master. Not a car problem, and
                                    _editions_come_from_masters' business.

Counting those together would produce a number nobody can act on.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.doctor import Report, _catalogued_tracks_reach_the_car


@pytest.fixture
def cfg(tmp_path) -> MusicConfig:
    lib = tmp_path / "Libraries" / "ALAC_Library"
    arc = tmp_path / "Libraries" / "ALAC-Archival"
    lib.mkdir(parents=True)
    arc.mkdir(parents=True)
    db = tmp_path / "musaeus.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE archive (file_path TEXT, status TEXT, car_export_path TEXT)")
    conn.commit()
    conn.close()
    return MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=lib, db_path=db,
        alac_archive=arc,
    )


def _row(cfg, name: str, car: str = "", *, master: bool = True) -> Path:
    p = Path(cfg.alac_library) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0")
    if master:
        m = Path(cfg.alac_archive) / name
        m.parent.mkdir(parents=True, exist_ok=True)
        m.write_bytes(b"\0")
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("INSERT INTO archive VALUES (?, 'CATALOGUED', ?)", (str(p), car))
    conn.commit()
    conn.close()
    return p


def _only(rep: Report):
    found = [f for f in rep.findings if f.check == "car edition coverage"]
    assert len(found) == 1, f"expected one finding, got {found}"
    return found[0]


class TestACompleteEditionIsQuiet:
    def test_every_track_having_a_car_file_passes(self, cfg):
        _row(cfg, "A/Al/one.m4a", car="/car/one.m4a")
        _row(cfg, "A/Al/two.m4a", car="/car/two.m4a")
        rep = Report()
        _catalogued_tracks_reach_the_car(cfg, rep)
        assert _only(rep).level == "ok"

    def test_an_empty_catalogue_is_not_a_finding(self, cfg):
        rep = Report()
        _catalogued_tracks_reach_the_car(cfg, rep)
        assert _only(rep).level == "ok"


class TestAGapThatCouldBeBuiltIsReported:
    def test_a_missing_car_file_with_a_master_present_warns(self, cfg):
        _row(cfg, "A/Al/has.m4a", car="/car/has.m4a")
        _row(cfg, "A/Al/gap.m4a")
        rep = Report()
        _catalogued_tracks_reach_the_car(cfg, rep)
        f = _only(rep)
        assert f.level == "warn"
        assert f.count == 1
        assert "50% complete" in f.detail

    def test_the_count_is_buildable_rows_not_all_missing_rows(self, cfg):
        """The number has to be the number a build could fix, or it is a
        figure nobody can act on."""
        _row(cfg, "A/Al/ok.m4a", car="/car/ok.m4a")
        _row(cfg, "A/Al/buildable.m4a")
        _row(cfg, "A/Al/nomaster.m4a", master=False)
        rep = Report()
        _catalogued_tracks_reach_the_car(cfg, rep)
        f = _only(rep)
        assert f.level == "warn"
        assert f.count == 1, "only the row with a master is buildable"


class TestAMissingMasterIsSomebodyElsesProblem:
    def test_gaps_with_no_master_do_not_warn_here(self, cfg):
        """Nothing can be built from a missing master, so reporting it as a
        car gap sends you to re-run a build that cannot help."""
        _row(cfg, "A/Al/ok.m4a", car="/car/ok.m4a")
        _row(cfg, "A/Al/orphan.m4a", master=False)
        rep = Report()
        _catalogued_tracks_reach_the_car(cfg, rep)
        f = _only(rep)
        assert f.level == "ok"
        assert "no master to build from" in f.detail


class TestItDoesNotBreakTheCaller:
    def test_no_database_is_skipped(self, cfg):
        Path(cfg.db_path).unlink()
        rep = Report()
        _catalogued_tracks_reach_the_car(cfg, rep)
        assert _only(rep).level == "ok"

    def test_a_catalogue_with_no_car_column_is_skipped_not_warned(self, cfg):
        """A schema that predates car_export_path has no car edition to be
        incomplete. Warning on it turned a minimal fixture's clean report into
        a WARN and took two existing doctor tests down -- the same shape as
        the meta_dir assumption that once broke 28 of them."""
        conn = sqlite3.connect(cfg.db_path)
        conn.execute("DROP TABLE archive")
        conn.execute("CREATE TABLE archive (file_path TEXT, status TEXT)")
        conn.commit()
        conn.close()
        rep = Report()
        _catalogued_tracks_reach_the_car(cfg, rep)
        f = _only(rep)
        assert f.level == "ok"
        assert "skipped" in f.detail

    def test_a_missing_archive_table_warns_rather_than_raising(self, cfg):
        """A genuinely unreadable catalogue is still a finding -- 'skipped'
        must not become the answer to every error."""
        conn = sqlite3.connect(cfg.db_path)
        conn.execute("DROP TABLE archive")
        conn.commit()
        conn.close()
        rep = Report()
        _catalogued_tracks_reach_the_car(cfg, rep)
        assert _only(rep).level == "warn"

    def test_a_config_with_no_edition_roots_does_not_raise(self, cfg):
        """doctor runs against whatever config it is handed, and a minimal one
        need not carry the edition paths. The first cross-authority check
        assumed meta_dir and took 28 tests down with it."""
        _row(cfg, "A/Al/gap.m4a")
        object.__setattr__(cfg, "alac_archive", None)
        rep = Report()
        _catalogued_tracks_reach_the_car(cfg, rep)
        assert _only(rep).level in ("ok", "warn")
