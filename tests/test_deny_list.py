"""
Tests for DenyListStage.

Removing a file does not stop it coming back. Traced 2026-08-24: a
finalized-hash ledger hit is only acted on when a *live* file backs it,
because CrossDupeStage verifies the path exists first — which is the
section 4.17 fix and must stay. So a purged knock-off dropped back into
the INBOX was ingested as if never seen. 96 ledger entries named removed
content and looked like protection while providing none.

Two properties matter most here and are pinned below: it must quarantine
rather than delete (a false positive must be recoverable), and it must
never touch already-catalogued music (retroactively quarantining the
owner's library would be far worse than a re-ingest slipping through).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from musaeus.config import MusicConfig
from musaeus.context import RunContext
from musaeus.db import deny_hash, ensure_deny_list, open_db, open_hash_index, upsert_archive
from musaeus.stages.deny_list import DenyListStage


@pytest.fixture
def vault(tmp_path: Path) -> MusicConfig:
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
    for d in (cfg.inbox, cfg.quarantine, cfg.alac_library, cfg.hash_index_path.parent):
        d.mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def ctx(vault) -> RunContext:
    return RunContext.new(vault, open_db(vault.db_path), dry_run=False)


def _deny(ctx, h, name="Knock Off - Hey Jude.m4a"):
    conn = open_hash_index(ctx.config.hash_index_path)
    ensure_deny_list(conn)
    deny_hash(conn, h, "removed by owner decision", name)
    conn.commit()
    conn.close()


def _incoming(ctx, name, audio_hash, status="HASHED"):
    p = ctx.config.inbox / name
    p.write_bytes(b"audio")
    upsert_archive(ctx.conn, {"file_path": str(p), "status": status,
                              "audio_hash": audio_hash, "artist": "X", "title": "Y"})
    ctx.conn.commit()
    return p


class TestARefusedFileIsQuarantinedNotDeleted:
    def test_the_file_still_exists_afterwards(self, ctx):
        _deny(ctx, "h" * 64)
        src = _incoming(ctx, "returning.m4a", "h" * 64)

        DenyListStage().run(ctx)

        assert not src.exists(), "should have been moved out of the inbox"
        moved = ctx.config.quarantine / "denied" / "returning.m4a"
        assert moved.exists(), "a deny-list false positive must be recoverable"

    def test_the_row_is_quarantined_and_the_reason_recorded(self, ctx):
        _deny(ctx, "h" * 64)
        _incoming(ctx, "returning.m4a", "h" * 64)

        DenyListStage().run(ctx)

        row = ctx.conn.execute("SELECT status, file_path FROM archive").fetchone()
        assert row["status"] == "QUARANTINED"
        ev = ctx.conn.execute(
            "SELECT event_type, new_value FROM events WHERE event_type='DENIED_REINGEST'"
        ).fetchone()
        assert ev is not None
        assert "owner decision" in ev["new_value"]


class TestItLeavesEverythingElseAlone:
    def test_an_undenied_file_passes_through(self, ctx):
        _deny(ctx, "h" * 64)
        src = _incoming(ctx, "wanted.m4a", "a" * 64)

        DenyListStage().run(ctx)

        assert src.exists()
        assert ctx.conn.execute("SELECT status FROM archive").fetchone()["status"] == "HASHED"

    def test_already_catalogued_music_is_never_touched(self, ctx):
        # The owner's library is not this stage's business. Retroactively
        # quarantining held music because of a list entry would be a far
        # worse failure than a re-ingest slipping through.
        _deny(ctx, "h" * 64)
        src = _incoming(ctx, "held.m4a", "h" * 64, status="CATALOGUED")

        DenyListStage().run(ctx)

        assert src.exists()
        assert ctx.conn.execute("SELECT status FROM archive").fetchone()["status"] == "CATALOGUED"

    def test_an_empty_deny_list_does_nothing(self, ctx):
        src = _incoming(ctx, "anything.m4a", "z" * 64)
        result = DenyListStage().run(ctx)
        assert src.exists()
        assert result.files_changed == 0


class TestDryRunWritesNothing:
    def test_dry_run_leaves_the_file_and_row_alone(self, ctx):
        _deny(ctx, "h" * 64)
        src = _incoming(ctx, "returning.m4a", "h" * 64)

        result = DenyListStage().dry_run(ctx)

        assert src.exists()
        assert ctx.conn.execute("SELECT status FROM archive").fetchone()["status"] == "HASHED"
        assert result.files_changed == 1, "it should still report what it would refuse"


class TestVerifyEffect:
    def test_a_denied_hash_surviving_into_the_library_is_reported(self, ctx):
        _deny(ctx, "h" * 64)
        _incoming(ctx, "leaked.m4a", "h" * 64, status="CATALOGUED")

        problems = DenyListStage().verify_effect(ctx, DenyListStage()._make_result())

        assert any("denied audio hash" in p for p in problems)

    def test_a_clean_library_reports_nothing(self, ctx):
        _deny(ctx, "h" * 64)
        _incoming(ctx, "fine.m4a", "a" * 64, status="CATALOGUED")

        assert DenyListStage().verify_effect(ctx, DenyListStage()._make_result()) == []


class TestTheLogIdentifiesTheFileBeforeScholarHasRun:
    """This stage runs before Scholar, so artist/title are empty on a
    freshly ingested row. A live run on 2026-08-24 logged
    "refusing ? — <filename>", which is noise where the filename alone
    reads fine."""

    def test_label_falls_back_to_the_filename(self, ctx, caplog):
        import logging

        _deny(ctx, "h" * 64)
        _incoming(ctx, "returning.m4a", "h" * 64)
        ctx.conn.execute("UPDATE archive SET artist=NULL, title=NULL")
        ctx.conn.commit()

        with caplog.at_level(logging.INFO, logger="musaeus.stages.deny_list"):
            DenyListStage().run(ctx)

        line = next(r.getMessage() for r in caplog.records if "refusing" in r.getMessage())
        assert "returning.m4a" in line
        assert "?" not in line

    def test_a_known_artist_and_title_are_used_when_present(self, ctx, caplog):
        import logging

        _deny(ctx, "h" * 64)
        _incoming(ctx, "returning.m4a", "h" * 64)

        with caplog.at_level(logging.INFO, logger="musaeus.stages.deny_list"):
            DenyListStage().run(ctx)

        line = next(r.getMessage() for r in caplog.records if "refusing" in r.getMessage())
        assert "X — Y" in line


class TestTheMoveAndTheRowStayInStep:
    """A move cannot be rolled back; a database write can.

    This stage was written move-first, which is the same shape that left 86
    files relocated with the database rolled back behind them in a script
    the same week. See scope §4.25.
    """

    def test_a_failed_move_leaves_the_row_untouched(self, ctx, monkeypatch):
        import shutil as _shutil

        _deny(ctx, "h" * 64)
        src = _incoming(ctx, "returning.m4a", "h" * 64)

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(_shutil, "move", boom)
        monkeypatch.setattr("musaeus.stages.deny_list.shutil.move", boom, raising=False)

        DenyListStage().run(ctx)

        row = ctx.conn.execute("SELECT status, file_path FROM archive").fetchone()
        assert row["status"] == "HASHED", "the row must not claim a quarantine that failed"
        assert row["file_path"] == str(src)
        assert src.exists()

    def test_a_name_collision_in_quarantine_does_not_overwrite(self, ctx):
        # Two different recordings can share a filename. shutil.move
        # overwrites, so the destination has to be made unique first.
        _deny(ctx, "h" * 64)
        _deny(ctx, "g" * 64)
        (ctx.config.quarantine / "denied").mkdir(parents=True, exist_ok=True)
        (ctx.config.quarantine / "denied" / "same.m4a").write_bytes(b"first")

        _incoming(ctx, "same.m4a", "h" * 64)
        DenyListStage().run(ctx)

        kept = list((ctx.config.quarantine / "denied").glob("*.m4a"))
        assert len(kept) == 2, "the pre-existing file must survive"
        assert (ctx.config.quarantine / "denied" / "same.m4a").read_bytes() == b"first"


class TestQuarantineClearsTheFinalizedMarker:
    """`finalized_at` means "this row is filed in ALAC-Library".

    A file finalized in an earlier batch can be re-ingested and denied.
    Quarantine moved it and set status, but left finalized_at standing, so
    the row claimed to be in the library while sitting in QUARANTINE.
    AuditStage checks exactly that pair and reported 5 such rows on the
    live vault 2026-08-26 -- one more every batch that denies a
    previously-finalized file, each one permanent.
    """

    def test_a_previously_finalized_row_loses_the_marker(self, ctx):
        _deny(ctx, "f" * 64)
        src = _incoming(ctx, "returning.m4a", "f" * 64)
        # The distinguishing state: finalized in an earlier batch.
        ctx.conn.execute(
            "UPDATE archive SET finalized_at='2026-08-25 19:01:40' WHERE file_path=?",
            (str(src),),
        )
        ctx.conn.commit()

        DenyListStage().run(ctx)

        row = ctx.conn.execute(
            "SELECT status, file_path, finalized_at FROM archive"
        ).fetchone()
        assert row["status"] == "QUARANTINED"
        assert row["finalized_at"] is None, "a quarantined row is not finalized"
        # And the audit invariant this exists to protect:
        assert "/ALAC-Library/" not in row["file_path"]

    def test_a_never_finalized_row_is_unaffected(self, ctx):
        _deny(ctx, "g" * 64)
        _incoming(ctx, "fresh.m4a", "g" * 64)

        DenyListStage().run(ctx)

        row = ctx.conn.execute("SELECT status, finalized_at FROM archive").fetchone()
        assert row["status"] == "QUARANTINED"
        assert row["finalized_at"] is None
