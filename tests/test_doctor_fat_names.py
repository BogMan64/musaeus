"""doctor reports library names a FAT32 stick cannot hold (Grey, 2026-10-03).

The car stick is FAT32: it refuses \\ : * ? " < > |, drops a trailing dot or
space, and ignores letter case. On 2026-10-03 the masters held 26 folder pairs
differing only by case. doctor reports every library -- masters and each
edition -- so a name that would break the stick shows up before a copy does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.doctor import Report, _names_safe_on_fat


@pytest.fixture
def cfg(tmp_path) -> MusicConfig:
    libs = tmp_path / "Libraries"
    return MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "Q",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=libs / "ALAC_Library",
        db_path=tmp_path / "musaeus.db",
        alac_archive=libs / "ALAC-Archival",
        car_library=libs / "CAR_Library",
        iphone_library=libs / "iPHONE_Library",
    )


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0")
    return p


def _findings(cfg) -> dict[str, tuple[str, str]]:
    rep = Report()
    _names_safe_on_fat(cfg, rep)
    return {f.check: (f.level, f.detail) for f in rep.findings}


def test_clean_libraries_are_ok(cfg):
    _touch(cfg.alac_archive / "Rock/Sweet, The/Unsorted/Sweet, The - Co-Co.m4a")
    _touch(cfg.car_library / "Sweet, The/Unsorted/Sweet, The - Co-Co.m4a")
    found = _findings(cfg)
    assert found["FAT32-safe names: masters"][0] == "ok"
    assert found["FAT32-safe names: Car"][0] == "ok"


def test_folders_differing_only_by_case_are_reported(cfg):
    _touch(cfg.alac_archive / "Rock/Pink Floyd/The Dark Side Of The Moon/Pink Floyd - Time.m4a")
    _touch(cfg.alac_archive / "Rock/Pink Floyd/The Dark Side of the Moon/Pink Floyd - Money.m4a")
    level, detail = _findings(cfg)["FAT32-safe names: masters"]
    assert level == "warn"
    assert "1 folder pair" in detail and "differ only by letter case" in detail


def test_files_that_would_overwrite_each_other_are_reported(cfg):
    _touch(cfg.car_library / "Usher/Unsorted/Usher - Yeah.m4a")
    _touch(cfg.car_library / "USHER/Unsorted/Usher - Yeah.m4a")
    level, detail = _findings(cfg)["FAT32-safe names: Car"]
    assert level == "warn"
    assert "would overwrite" in detail


def test_a_forbidden_character_is_reported(cfg):
    _touch(cfg.iphone_library / "AC:DC/Back In Black/AC:DC - Hells Bells.m4a")
    level, detail = _findings(cfg)["FAT32-safe names: iPhone"]
    assert level == "warn" and "cannot hold" in detail


def test_a_missing_library_is_skipped_not_failed(cfg):
    _touch(cfg.alac_archive / "Rock/A/B/A - B.m4a")
    found = _findings(cfg)
    assert "FAT32-safe names: iPhone" not in found
    assert all(level != "fail" for level, _ in found.values())
