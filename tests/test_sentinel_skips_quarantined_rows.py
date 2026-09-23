"""A quarantined file must not be re-hashed forever.

`_get_pending` selected `audio_hash IS NULL` with no status filter. A file
quarantined for failing to decode has a NULL audio_hash *because* it could
not be hashed -- so Sentinel picked it up, re-attempted the decode that had
already failed, failed again, and reported errors=1. The stage then FAILED on
every future run, permanently, over a file the pipeline had already handled
correctly.

Observed 2026-09-21 the first time the new CorruptStage decode gate
quarantined a truncated arrival. The defect pre-dated the gate; the gate just
made it fire reliably, because quarantining undecodable files is its job.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.db import open_db, upsert_archive
from musaeus.stages.sentinel import _get_pending


@pytest.fixture
def conn(tmp_path: Path):
    cfg = MusicConfig(
        vault_root=tmp_path,
        inbox=tmp_path / "INBOX",
        staging=tmp_path / "STAGING",
        quarantine=tmp_path / "QUARANTINE",
        runs_root=tmp_path / "RUNS",
        meta_dir=tmp_path / "MetaData",
        alac_library=tmp_path / "ALAC-Library",
        db_path=tmp_path / "musaeus.db",
    )
    return open_db(cfg.db_path)


def _row(conn, path: str, status: str, audio_hash: str | None) -> None:
    upsert_archive(conn, {"file_path": path, "status": status, "audio_hash": audio_hash})
    conn.commit()


def _paths(conn) -> set[str]:
    return {r["file_path"] for r in _get_pending(conn)}


@pytest.mark.parametrize("status", ["QUARANTINED", "DELETED"])
def test_a_row_with_no_hash_is_not_rehashed_when_it_is_not_live(conn, status) -> None:
    _row(conn, f"/vault/QUARANTINE/corrupted/{status}.m4a", status, None)
    assert _paths(conn) == set(), (
        f"{status} row with NULL audio_hash was selected for hashing; "
        "re-decoding it fails by definition and fails the stage every run"
    )


def test_a_ghost_is_still_returned_so_a_returning_file_can_recover(conn) -> None:
    """GHOST is NOT excluded: re-scanning it is how recovery happens."""
    _row(conn, "/vault/ALAC-Library/back.m4a", "GHOST", None)
    assert _paths(conn) == {"/vault/ALAC-Library/back.m4a"}


def test_a_genuine_pending_row_is_still_returned(conn) -> None:
    _row(conn, "/vault/INBOX/new.m4a", "PENDING", None)
    assert _paths(conn) == {"/vault/INBOX/new.m4a"}


def test_a_live_row_missing_its_hash_is_still_returned(conn) -> None:
    """The other half of the original intent must survive the fix."""
    _row(conn, "/vault/ALAC-Library/live.m4a", "CATALOGUED", None)
    assert _paths(conn) == {"/vault/ALAC-Library/live.m4a"}


def test_a_hashed_live_row_is_not_rehashed(conn) -> None:
    _row(conn, "/vault/ALAC-Library/done.m4a", "CATALOGUED", "abc123")
    assert _paths(conn) == set()
