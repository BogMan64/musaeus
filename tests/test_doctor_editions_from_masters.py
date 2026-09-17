"""doctor asks whether an edition is being built from another edition.

The scope has always said "Masters are never baked. Each edition bakes
exactly once, from the masters. No edition is ever built from another." On
2026-09-14 three quarters of the car edition was being built from the -18
LUFS lossless edition, and nothing noticed for as long as the rule lived
only in prose: a baked row's file_path follows the EDITION, and the code
that staged those rows read file_path while its own docstring said "master".

A rule only a person can check is a rule that gets broken between the times
that person looks. This is that rule made mechanical.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.doctor import Report, _editions_come_from_masters


@pytest.fixture
def cfg(tmp_path) -> MusicConfig:
    lib = tmp_path / "Libraries" / "ALAC_Library"
    arc = tmp_path / "Libraries" / "ALAC-Archival"
    (lib / "A" / "Al").mkdir(parents=True)
    (arc / "A" / "Al").mkdir(parents=True)
    db = tmp_path / "musaeus.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE archive (file_path TEXT, status TEXT)")
    conn.commit()
    conn.close()
    return MusicConfig(
        vault_root=tmp_path, inbox=tmp_path / "INBOX", staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q", runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData", alac_library=lib, db_path=db,
        alac_archive=arc,
    )


def _row(cfg, path: Path) -> None:
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("INSERT INTO archive (file_path, status) VALUES (?, 'CATALOGUED')", (str(path),))
    conn.commit()
    conn.close()


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0")
    return p


class TestItPassesWhenEveryRowHasAMaster:
    def test_a_baked_row_whose_master_exists_is_fine(self, cfg):
        _touch(cfg.alac_library / "A" / "Al" / "t.m4a")
        _touch(cfg.alac_archive / "A" / "Al" / "t.m4a")
        _row(cfg, cfg.alac_library / "A" / "Al" / "t.m4a")

        rep = Report()
        _editions_come_from_masters(cfg, rep)
        f = rep.findings[-1]
        assert f.level == "ok"
        assert "resolve to a master" in f.detail


class TestItWarnsWhenTheRuleCannotBeKept:
    def test_a_row_with_no_master_is_reported(self, cfg):
        """Not a crash and not silence: an edition built now would take this
        track from its -18 library copy, which is the thing forbidden."""
        _touch(cfg.alac_library / "A" / "Al" / "orphan.m4a")  # no master written
        _row(cfg, cfg.alac_library / "A" / "Al" / "orphan.m4a")

        rep = Report()
        _editions_come_from_masters(cfg, rep)
        f = rep.findings[-1]
        assert f.level == "warn"
        assert f.count == 1
        assert "scope forbids" in f.detail

    def test_the_count_is_the_number_of_rows_not_a_boolean(self, cfg):
        for n in range(3):
            p = _touch(cfg.alac_library / "A" / "Al" / f"o{n}.m4a")
            _row(cfg, p)

        rep = Report()
        _editions_come_from_masters(cfg, rep)
        assert rep.findings[-1].count == 3


class TestItDegradesRatherThanExploding:
    def test_a_missing_masters_tree_warns_and_does_not_raise(self, cfg):
        import shutil

        shutil.rmtree(cfg.alac_archive)
        rep = Report()
        _editions_come_from_masters(cfg, rep)
        assert rep.findings[-1].level == "warn"
        assert "not found" in rep.findings[-1].detail

    def test_an_unconfigured_vault_is_skipped_not_failed(self, cfg):
        """doctor runs against whatever config it is handed, including older
        ones that predate these fields."""
        cfg.alac_archive = None
        rep = Report()
        _editions_come_from_masters(cfg, rep)
        assert rep.findings[-1].level == "ok"
        assert "skipped" in rep.findings[-1].detail
