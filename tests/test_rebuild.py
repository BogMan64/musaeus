"""
Tests for musaeus.rebuild — the DISABLED event-log rebuild.

The previous suite here had 13 passing tests exercising the replay logic,
and every one of them passed against code that could never have worked on
real data. They fed it the event-type names the implementation expected
(FILE_REGISTERED, FILE_HASHED, FILE_CATALOGUED, ...), none of which any
MUSAEUS stage has ever emitted. The tests validated the implementation's
own assumptions rather than reality, so the module looked well-covered
while being entirely dead code sitting in front of a DELETE FROM archive.

These tests replace them: they assert the module refuses to run, and they
pin the two facts that make it unfixable so a future session doesn't
"helpfully" re-enable it by renaming the event constants.
"""

from __future__ import annotations

import pytest

from musaeus.db import log_event, open_db
from musaeus.rebuild import (
    RebuildDisabledError,
    cmd_rebuild_db,
    rebuild_archive_from_events,
)


class TestRebuildIsDisabled:
    def test_rebuild_raises_rather_than_running(self, tmp_path):
        conn = open_db(tmp_path / "t.db")
        with pytest.raises(RebuildDisabledError):
            rebuild_archive_from_events(conn)

    def test_dry_run_also_refuses(self, tmp_path):
        # Nothing safe to preview: printing a plausible plan would imply
        # the real run works.
        conn = open_db(tmp_path / "t.db")
        with pytest.raises(RebuildDisabledError):
            rebuild_archive_from_events(conn, dry_run=True)

    def test_it_does_not_touch_the_archive_table(self, tmp_path):
        """The dangerous part was DELETE FROM archive running *first*."""
        db = tmp_path / "t.db"
        conn = open_db(db)
        conn.execute(
            "INSERT INTO archive (file_path, status, artist) VALUES (?,?,?)",
            ("/x/y.m4a", "CATALOGUED", "Beatles, The"),
        )
        conn.commit()
        with pytest.raises(RebuildDisabledError):
            rebuild_archive_from_events(conn)
        assert conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0] == 1

    def test_cli_entry_point_returns_refused_exit_code(self, capsys):
        # 2 == "refused, did not run", matching the P0-02 dry-run guard,
        # rather than 1 == "ran and failed".
        assert cmd_rebuild_db() == 2
        assert cmd_rebuild_db(dry_run=True) == 2
        assert "DISABLED" in capsys.readouterr().err


class TestWhyItCannotBeFixed:
    """Pins the two independent reasons, so renaming constants isn't
    mistaken for a fix."""

    def test_emitted_event_names_do_not_match_what_replay_expected(self, tmp_path):
        conn = open_db(tmp_path / "t.db")
        # Names a real stage actually emits (see musaeus/stages/*.py).
        for et in ("INGEST", "HASH_COMPUTED", "METADATA_EXTRACTED", "FINALIZE_MOVE"):
            log_event(conn, run_id="r", event_type=et, file_path="/x/y.m4a", stage="s")
        conn.commit()
        emitted = {r[0] for r in conn.execute("SELECT DISTINCT event_type FROM events")}
        replay_expected = {
            "FILE_REGISTERED",
            "FILE_HASHED",
            "FILE_CATALOGUED",
            "STATUS_CHANGE",
            "FIELD_UPDATE",
            "LUFS_MEASURED",
            "RG_TAGGED",
            "CAR_EXPORTED",
            "FILE_GHOST",
            "FILE_REMOVED",
        }
        assert emitted.isdisjoint(replay_expected)

    def test_event_log_is_lossy_so_hashes_cannot_be_recovered(self, tmp_path):
        """sentinel.py logs a display-truncated hash, not the real value."""
        conn = open_db(tmp_path / "t.db")
        real = "a" * 64
        log_event(
            conn,
            run_id="r",
            event_type="HASH_COMPUTED",
            file_path="/x/y.m4a",
            new_value=f"{real[:16]}…",  # what sentinel actually writes
            stage="sentinel",
        )
        conn.commit()
        stored = conn.execute("SELECT new_value FROM events").fetchone()[0]
        assert stored.endswith("…")
        assert len(stored) < len(real)
        assert stored.rstrip("…") != real
