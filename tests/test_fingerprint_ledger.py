"""A fingerprint must outlive the catalogue that recorded it.

The 2026-08-30 AcoustID pass spent 21 HOURS fingerprinting 7,545 of 10,656
files before it was stopped for holding the write lock. Every result went
into `archive`. `archive` was later rebuilt, and measured on 2026-09-16
acousticid_recording, acousticid_score and acousticid_checked_at were
non-null on ZERO rows -- twenty-one hours gone, with no trace that it had
ever happened.

So the fingerprint lives in the LEDGER (_db_backups/hash_index.db), beside
denied_hashes, for the same reason and after the same lesson: the catalogue
can be rebuilt and this cannot be recomputed cheaply.

Keyed on audio_hash, not file_path, because that is what a fingerprint is a
property OF. In one day this library had its artists re-filed, its articles
moved, its album folders re-cased and 84 artist folders renamed. Every one of
those changes moves a file_path. None of them changes the audio.
"""

from __future__ import annotations

import sqlite3

import pytest

from musaeus.db import ensure_fingerprints, lookup_fingerprint, remember_fingerprint


@pytest.fixture
def led():
    c = sqlite3.connect(":memory:")
    ensure_fingerprints(c)
    return c


class TestTheLedgerRemembers:
    def test_a_fingerprint_round_trips(self, led):
        remember_fingerprint(led, "h1", "FP", 210.5, "rec", 0.9, "2026-08-30")
        r = lookup_fingerprint(led, "h1")
        assert r["chromaprint"] == "FP"
        assert r["acousticid_recording"] == "rec"
        assert r["checked_at"] == "2026-08-30"

    def test_an_unknown_hash_is_none(self, led):
        assert lookup_fingerprint(led, "nope") is None

    def test_ensure_is_idempotent(self, led):
        ensure_fingerprints(led)
        remember_fingerprint(led, "h1", "FP", 1.0, None, None, None)
        assert lookup_fingerprint(led, "h1") is not None


class TestTheTwoHalvesHaveDifferentLifetimes:
    """The local fingerprint is true whatever the network did; the AcoustID
    answer asserts that AcoustID replied. A row may carry one without the
    other, and filling in the second must not erase the first."""

    def test_a_later_answer_does_not_erase_the_fingerprint(self, led):
        remember_fingerprint(led, "h1", "FPDATA", 211.0, None, None, None)
        remember_fingerprint(led, "h1", None, None, "rec-uuid", 0.94, "2026-09-16")
        r = lookup_fingerprint(led, "h1")
        assert r["chromaprint"] == "FPDATA", "the expensive half was overwritten with NULL"
        assert r["chromaprint_duration"] == 211.0
        assert r["acousticid_recording"] == "rec-uuid"

    def test_a_fingerprint_with_no_answer_is_still_stored(self, led):
        """One network timeout used to retire a row for ever. The fingerprint
        is worth keeping even when the lookup failed."""
        remember_fingerprint(led, "h1", "FPDATA", 211.0, None, None, None)
        r = lookup_fingerprint(led, "h1")
        assert r["chromaprint"] == "FPDATA"
        assert r["acousticid_recording"] is None
        assert r["checked_at"] is None

    def test_re_answering_updates_rather_than_duplicating(self, led):
        remember_fingerprint(led, "h1", "FP", 1.0, "old", 0.5, "2026-01-01")
        remember_fingerprint(led, "h1", "FP", 1.0, "new", 0.9, "2026-02-02")
        n = led.execute("SELECT COUNT(*) FROM fingerprints WHERE audio_hash='h1'").fetchone()[0]
        assert n == 1
        assert lookup_fingerprint(led, "h1")["acousticid_recording"] == "new"


class TestRestoringARebuiltCatalogue:
    def test_a_known_hash_is_restored_without_refingerprinting(self, tmp_path):
        """The whole point: a rebuilt catalogue costs seconds, not 21 hours."""
        import musaeus.stages.acousticid as A

        (tmp_path / "_db_backups").mkdir()
        db = tmp_path / "musaeus.db"
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE archive (file_path TEXT, audio_hash TEXT, status TEXT, "
                  "chromaprint TEXT, chromaprint_duration REAL, acousticid_recording TEXT, "
                  "acousticid_score REAL, acousticid_checked_at TEXT)")
        c.execute("INSERT INTO archive (file_path,audio_hash,status) "
                  "VALUES ('/a.m4a','HASH1','CATALOGUED')")
        c.commit()

        lp = tmp_path / "_db_backups" / "hash_index.db"
        lc = sqlite3.connect(lp)
        ensure_fingerprints(lc)
        remember_fingerprint(lc, "HASH1", "FPDATA", 211.0, "rec-uuid", 0.97, "2026-08-30")
        lc.commit()
        lc.close()

        ctx = type("Ctx", (), {"conn": c,
                               "config": type("C", (), {"vault_root": tmp_path})()})()
        assert A._restore_from_ledger(ctx) == 1
        row = c.execute("SELECT chromaprint, acousticid_recording, acousticid_checked_at "
                        "FROM archive").fetchone()
        assert row == ("FPDATA", "rec-uuid", "2026-08-30")

    def test_a_missing_ledger_is_not_an_error(self, tmp_path):
        """A cache that is not there is a missing optimisation, not a fault.
        A stage that failed because of one would be worse than no cache."""
        import musaeus.stages.acousticid as A

        db = tmp_path / "musaeus.db"
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE archive (file_path TEXT, audio_hash TEXT, status TEXT, "
                  "acousticid_checked_at TEXT)")
        c.commit()
        ctx = type("Ctx", (), {"conn": c,
                               "config": type("C", (), {"vault_root": tmp_path})()})()
        assert A._restore_from_ledger(ctx) == 0

    def test_a_catalogue_without_audio_hash_is_skipped_quietly(self, tmp_path):
        import musaeus.stages.acousticid as A

        (tmp_path / "_db_backups").mkdir()
        lc = sqlite3.connect(tmp_path / "_db_backups" / "hash_index.db")
        ensure_fingerprints(lc)
        remember_fingerprint(lc, "HASH1", "FP", 1.0, "rec", 0.9, "2026-08-30")
        lc.commit()
        lc.close()
        c = sqlite3.connect(tmp_path / "musaeus.db")
        c.execute("CREATE TABLE archive (file_path TEXT, status TEXT)")
        c.commit()
        ctx = type("Ctx", (), {"conn": c,
                               "config": type("C", (), {"vault_root": tmp_path})()})()
        assert A._restore_from_ledger(ctx) == 0


class TestTheStageActuallyUsesIt:
    def test_the_stage_imports_sqlite3(self):
        """The helpers call sqlite3.connect. The module did not import it, and
        nothing noticed because the failure only happens at RUN time -- which
        here means hours into a fingerprint pass."""
        import musaeus.stages.acousticid as A
        assert hasattr(A, "sqlite3")

    def test_results_are_written_to_the_ledger_not_only_to_archive(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "musaeus" / "stages"
               / "acousticid.py").read_text()
        assert "_remember_in_ledger(" in src
        assert "_restore_from_ledger(" in src
