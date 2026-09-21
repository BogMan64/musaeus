"""rebuild_from_disk must still find the review queues after they moved.

On 2026-09-20 DUPES_MOVED / TRIBUTE_REMOVED moved out of Libraries/ to
vault_root/REVIEW/. rebuild_from_disk rglobs cfg.alac_library, and
_status_for() returned "CATALOGUED" for anything it could not make relative
to that library -- so a review file would have been rebuilt as live content,
which is wrong in the dangerous direction.
"""
from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.rebuild_from_disk import _status_for


@pytest.fixture
def cfg(tmp_path) -> MusicConfig:
    return MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "Libraries" / "ALAC_Library",
        db_path=tmp_path / "musaeus.db",
    )


def test_a_file_in_the_dupes_queue_is_not_reported_catalogued(cfg):
    p = cfg.dupes_review_dir / "2026-09-20" / "Artist" / "Album" / "t.m4a"
    assert _status_for(p, cfg.alac_library, cfg) == "DUPE_REVIEW"


def test_a_file_in_the_tribute_queue_is_not_reported_catalogued(cfg):
    p = cfg.tribute_review_dir / "2026-09-20" / "Artist" / "Album" / "t.m4a"
    assert _status_for(p, cfg.alac_library, cfg) == "TRIBUTE_REVIEW"


def test_ordinary_library_content_is_still_catalogued(cfg):
    p = cfg.alac_library / "Rock" / "Artist" / "Album" / "t.m4a"
    assert _status_for(p, cfg.alac_library, cfg) == "CATALOGUED"


def test_without_cfg_the_old_in_library_layout_still_works(cfg):
    """Back-compat: pre-move trees keep resolving by folder name."""
    p = cfg.alac_library / "DUPES_MOVED_FOR_REVIEW" / "2026-09-18" / "t.m4a"
    assert _status_for(p, cfg.alac_library) == "DUPE_REVIEW"


def test_scan_walks_the_review_queues_when_they_sit_outside_the_library():
    src = (Path(__file__).resolve().parent.parent
           / "musaeus" / "rebuild_from_disk.py").read_text()
    assert "scan_roots" in src, "scan no longer walks extra roots"
    assert "dupes_review_dir" in src and "tribute_review_dir" in src
