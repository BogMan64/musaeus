"""TributeQuarantine runs inside Act 1, and proves its moves landed.

Wired into DEFAULT_PIPELINE on 2026-09-01. It is the only stage that
REMOVES work rather than doing it, so its position is the whole point:
every stage after it -- GenreValidate's per-artist law, Act 2's dedup,
Act 3's transcode -- costs per row, and a row taken out here is cost not
paid. The 2026-09-01 INBOX arrived with 175 filename-flagged karaoke
files; transcoding those to ALAC before deciding to remove them is the
expensive order.

It also MOVES real music out of the library on a regex match, and now
does so without a human present. So it verifies like every other mover,
plus one check the others do not need: that the restore script exists.
"Moved, never deleted" is this stage's entire safety argument, and a
quarantine with no way back is a deletion with extra steps.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from musaeus.stages import DEFAULT_PIPELINE
from musaeus.stages.tribute_quarantine import TributeQuarantineStage

# ── Position in the chain ─────────────────────────────────────────────────────


def _names() -> list[str]:
    return [s.__name__ for s in DEFAULT_PIPELINE]


def test_it_is_in_the_default_pipeline() -> None:
    assert "TributeQuarantineStage" in _names()


def test_it_runs_after_the_stages_that_settle_the_artist_name() -> None:
    """It matches on artist/title/album; those two stages are what make
    those columns canonical. Matching the raw tag would be matching a name
    the run is about to change."""
    n = _names()
    assert n.index("ArtistConsolidateStage") < n.index("TributeQuarantineStage")
    assert n.index("VariousArtistsFixStage") < n.index("TributeQuarantineStage")


def test_it_runs_before_genre_law_dedup_and_transcode() -> None:
    """Everything after it is cost paid per row. Junk must not reach the
    genre law, the dedup groups, or the encoder."""
    n = _names()
    for later in ("GenreValidateStage", "CanonicalizeStage", "DupeResolverStage"):
        assert n.index("TributeQuarantineStage") < n.index(later), (
            f"{later} would run on files this stage is about to remove"
        )


def test_scholar_has_already_set_the_status_it_selects_on() -> None:
    """It queries status='CATALOGUED'. Scholar is what sets that, so the
    stage would silently match nothing if it ran first."""
    n = _names()
    assert n.index("ScholarStage") < n.index("TributeQuarantineStage")


# ── Verification ──────────────────────────────────────────────────────────────


class _Ctx:
    def __init__(self, conn, review_dir: Path, run_id="run_test"):
        self.conn = conn
        self.run_id = run_id
        self.config = SimpleNamespace(tribute_review_dir=review_dir)

    def get(self, _key, default=None):
        return "2026-09-01"


class _Result:
    def __init__(self, files_changed=1):
        self.files_changed = files_changed


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE archive (file_path TEXT PRIMARY KEY, status TEXT)")
    c.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, run_id TEXT, stage TEXT, "
        "event_type TEXT, file_path TEXT, old_value TEXT, new_value TEXT)"
    )
    return c


def _quarantined(conn, source: Path, target: Path, status="TRIBUTE_REVIEW", run="run_test"):
    conn.execute("INSERT INTO archive (file_path, status) VALUES (?,?)", (str(target), status))
    conn.execute(
        "INSERT INTO events (run_id, stage, event_type, file_path, old_value, new_value) "
        "VALUES (?,?,?,?,?,?)",
        (run, "tribute-quarantine", "TRIBUTE_QUARANTINED", str(target), str(source), str(target)),
    )
    conn.commit()


def _scene(tmp_path: Path, *, moved=True, copied=False, restore=True):
    """Build a quarantine that did (or did not) happen properly."""
    review = tmp_path / "TRIBUTE_REVIEW"
    batch = review / "2026-09-01"
    batch.mkdir(parents=True)
    source = tmp_path / "lib" / "Karaoke Channel - Your Song.m4a"
    source.parent.mkdir(parents=True)
    target = batch / "Karaoke Channel" / "Your Song.m4a"
    target.parent.mkdir(parents=True)
    if moved:
        target.write_bytes(b"x")
    if copied or not moved:
        source.write_bytes(b"x")
    if restore:
        (batch / "restore_20260901T000000Z.sh").write_text("#!/usr/bin/env bash\n")
    return review, source, target


def test_a_quarantine_that_landed_verifies(conn, tmp_path: Path) -> None:
    review, source, target = _scene(tmp_path)
    _quarantined(conn, source, target)
    assert TributeQuarantineStage().verify_effect(_Ctx(conn, review), _Result()) == []


def test_a_move_that_did_not_happen_is_reported(conn, tmp_path: Path) -> None:
    review, source, target = _scene(tmp_path, moved=False)
    _quarantined(conn, source, target)
    problems = TributeQuarantineStage().verify_effect(_Ctx(conn, review), _Result())
    assert any("not at the new path" in p for p in problems)


def test_a_copy_masquerading_as_a_move_is_reported(conn, tmp_path: Path) -> None:
    """The library still holds the file it believes it quarantined."""
    review, source, target = _scene(tmp_path, copied=True)
    _quarantined(conn, source, target)
    problems = TributeQuarantineStage().verify_effect(_Ctx(conn, review), _Result())
    assert any("BOTH paths" in p for p in problems)


def test_a_row_left_catalogued_is_reported(conn, tmp_path: Path) -> None:
    """The DB would still count a quarantined file as library content."""
    review, source, target = _scene(tmp_path)
    _quarantined(conn, source, target, status="CATALOGUED")
    problems = TributeQuarantineStage().verify_effect(_Ctx(conn, review), _Result())
    assert any("still CATALOGUED" in p for p in problems)


def test_a_missing_restore_script_is_reported(conn, tmp_path: Path) -> None:
    """The files moved, the rows updated, the count was right — and there
    is no way back. That is a deletion with extra steps."""
    review, source, target = _scene(tmp_path, restore=False)
    _quarantined(conn, source, target)
    problems = TributeQuarantineStage().verify_effect(_Ctx(conn, review), _Result())
    assert any("no restore script" in p for p in problems)


def test_changes_claimed_with_no_events_is_a_problem_not_a_pass(conn, tmp_path: Path) -> None:
    review = tmp_path / "TRIBUTE_REVIEW"
    review.mkdir()
    problems = TributeQuarantineStage().verify_effect(_Ctx(conn, review), _Result(files_changed=7))
    assert problems and "no TRIBUTE_QUARANTINED" in problems[0]
