"""
P0-14 — the duplicates contract and the AcoustID insertion path.

Two defects are repaired here, and both were invisible to the existing
suite because nothing ever executed the statements against a real schema:

  1. AcousticIDStage UPDATEs five `archive` columns -- chromaprint,
     chromaprint_duration, acousticid_recording, acousticid_score,
     acousticid_checked_at -- that existed nowhere: not in `_SCHEMA`, not
     in `_MIGRATIONS`. The stage failed on its first file.
  2. Its duplicates INSERT named `type` and `created_at`; the table has
     `duplicate_type` and `staged_at`.

MUSAEUS_TODO §8 records AcoustID as "never run". As written it could not
have run. The first test class below is the regression that would have
said so.
"""

from __future__ import annotations

import inspect
import contextlib
import sqlite3
import tempfile
from pathlib import Path

import pytest

from musaeus.state import duplicates as duplicates_mod
from musaeus.stages import acousticid as acousticid_mod
from musaeus.db import open_db
from musaeus.network_policy import NetworkPolicy, get_gateway
from musaeus.state.duplicates import (
    DECISION_PENDING,
    DETECTOR_ACOUSTID,
    INSERTION_COLUMNS,
    DuplicateCandidate,
    DuplicateContractError,
    DuplicateRepository,
    canonical_pair,
)
from musaeus.state.migrator import migrate

EVIDENCE = {"algorithm": "chromaprint", "provider": "acoustid", "match": "exact"}


@pytest.fixture
def legacy_db(tmp_path) -> Path:
    """A database exactly as db.open_db() produces it, with legacy rows."""
    path = tmp_path / "musaeus.db"
    conn = open_db(path)
    try:
        conn.execute(
            "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, "
            "status, run_id, staged_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("grp-1", "/x/a.m4a", "EXACT", 1.0, "pending", "old-run", "2026-08-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO duplicates (group_id, file_path, duplicate_type, confidence, "
            "status, run_id, staged_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("grp-1", "/x/b.m4a", "EXACT", 1.0, "pending", "old-run", "2026-08-01T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def migrated(tmp_path, legacy_db) -> sqlite3.Connection:
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    migrate(legacy_db, recovery_root=recovery)
    conn = sqlite3.connect(str(legacy_db), isolation_level=None)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


# ── The two live defects ──────────────────────────────────────────────────────


class TestAcoustIDStageCanActuallyRun:
    def test_the_five_archive_columns_exist(self):
        """They were absent from _SCHEMA and _MIGRATIONS both. Asserted
        against a real open_db() database, because that is the only place
        the absence was observable."""
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_db(Path(tmp) / "t.db")
            try:
                columns = {r[1] for r in conn.execute("PRAGMA table_info(archive)")}
            finally:
                conn.close()
        for column in (
            "chromaprint",
            "chromaprint_duration",
            "acousticid_recording",
            "acousticid_score",
            "acousticid_checked_at",
        ):
            assert column in columns, f"AcousticIDStage writes {column} and it does not exist"

    def test_the_stages_archive_update_executes(self):
        """The stage's exact UPDATE, run against the real schema."""
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_db(Path(tmp) / "t.db")
            try:
                conn.execute("INSERT INTO archive (file_path) VALUES ('/x/a.m4a')")
                conn.execute(
                    """
                    UPDATE archive
                       SET chromaprint=?, chromaprint_duration=?, acousticid_recording=?,
                           acousticid_score=?, acousticid_checked_at=?
                     WHERE file_path=?
                    """,
                    ("AQAAA", 231.0, "rec-123", 0.97, "2026-08-23T22:00:00Z", "/x/a.m4a"),
                )
                row = conn.execute(
                    "SELECT acousticid_recording, acousticid_score FROM archive"
                ).fetchone()
            finally:
                conn.close()
        assert row[0] == "rec-123"
        assert row[1] == pytest.approx(0.97)

    def test_the_stages_duplicates_insert_executes(self):
        """The statement named `type` and `created_at`; the table has
        `duplicate_type` and `staged_at`."""
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_db(Path(tmp) / "t.db")
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO duplicates
                        (group_id, file_path, duplicate_type, confidence, status,
                         run_id, staged_at)
                    VALUES (?, ?, 'ACOUSTIC', ?, 'pending', ?, ?)
                    """,
                    ("acoustic_1", "/x/a.m4a", 0.93, "run-A", "2026-08-23T22:00:00Z"),
                )
                row = conn.execute("SELECT duplicate_type, run_id FROM duplicates").fetchone()
            finally:
                conn.close()
        assert row[0] == "ACOUSTIC"
        assert row[1] == "run-A", "run_id was never populated, leaving pairs unattributable"

    def test_the_stage_source_no_longer_names_columns_that_do_not_exist(self):
        """Guards the specific regression rather than the general shape."""
        source = inspect.getsource(acousticid_mod)
        assert "INSERT OR IGNORE INTO duplicates" in source
        insert = source[source.index("INSERT OR IGNORE INTO duplicates") :][:400]
        assert "duplicate_type" in insert
        assert "staged_at" in insert
        assert " type," not in insert
        assert "created_at" not in insert


# ── The typed contract ────────────────────────────────────────────────────────


class TestDuplicateContract:
    def test_the_table_has_exactly_the_declared_columns(self, migrated):
        columns = [r[1] for r in migrated.execute("PRAGMA table_info(duplicates)")]
        assert columns[0] == "id", "id must be first and database-generated"
        for column in INSERTION_COLUMNS:
            assert column in columns, f"DR-07 requires {column}"
        assert "evidence_identity" in columns

    def test_id_is_database_generated(self, migrated):
        repo = DuplicateRepository(migrated)
        first = repo.insert_acoustid_candidate(
            run_id="run-A",
            candidate_item_id="item-b",
            matched_item_id="item-a",
            provider_recording_id="rec-1",
            fingerprint_digest="fp-1",
            score=0.98,
            evidence=EVIDENCE,
        )
        assert isinstance(first, int) and first > 0

    def test_every_typed_field_is_persisted(self, migrated):
        repo = DuplicateRepository(migrated)
        repo.insert_acoustid_candidate(
            run_id="run-A",
            candidate_item_id="item-b",
            matched_item_id="item-a",
            provider_recording_id="rec-1",
            fingerprint_digest="fp-digest",
            score=0.98,
            evidence=EVIDENCE,
            created_at="2026-08-23T22:00:00Z",
        )
        row = repo.pending("run-A")[0]
        assert row["detector"] == DETECTOR_ACOUSTID
        assert row["provider_recording_id"] == "rec-1"
        assert row["fingerprint_digest"] == "fp-digest"
        assert row["score"] == pytest.approx(0.98)
        assert row["decision_status"] == DECISION_PENDING
        assert row["created_at"] == "2026-08-23T22:00:00Z"
        assert "chromaprint" in row["evidence_json"]

    def test_the_insert_statement_names_every_column_explicitly(self):
        """An insert that names its columns fails loudly against the wrong
        schema -- which is how both live defects would have been caught on
        the first run instead of never.

        The column list is parsed rather than substring-matched: the first
        version of this test asked whether "id," appeared in the text,
        which it does, inside "run_id,". Substring arithmetic on SQL is
        how you write an assertion that is right for the wrong reason."""
        source = inspect.getsource(duplicates_mod)
        statement = source[source.index("INSERT OR IGNORE INTO duplicates") :]
        column_list = statement[statement.index("(") + 1 : statement.index(")")]
        named = [c.strip() for c in column_list.replace("\n", " ").split(",") if c.strip()]

        assert set(INSERTION_COLUMNS) <= set(named), (
            f"missing from the insert: {sorted(set(INSERTION_COLUMNS) - set(named))}"
        )
        assert "id" not in named, "the database-generated id must not be supplied"
        assert "SELECT *" not in statement[:600]

        placeholders = statement[statement.index("VALUES") :]
        assert placeholders.count("?") >= len(named), "every named column needs a placeholder"


class TestCanonicalPairOrdering:
    def test_pairs_are_ordered_deterministically(self):
        assert canonical_pair("b", "a") == ("a", "b")
        assert canonical_pair("a", "b") == ("a", "b")

    def test_an_item_cannot_duplicate_itself(self):
        with pytest.raises(DuplicateContractError):
            canonical_pair("a", "a")

    def test_the_mirror_image_does_not_insert_twice_on_resume(self, migrated):
        """Without canonical ordering, a resumed run would insert the
        mirror of a row it already has and the duplicate count would grow
        on every resume -- the shape of a false-positive cascade."""
        repo = DuplicateRepository(migrated)
        args = {
            "run_id": "run-A",
            "provider_recording_id": "rec-1",
            "fingerprint_digest": "fp-1",
            "score": 0.98,
            "evidence": EVIDENCE,
        }
        first = repo.insert_acoustid_candidate(
            candidate_item_id="item-a", matched_item_id="item-b", **args
        )
        mirror = repo.insert_acoustid_candidate(
            candidate_item_id="item-b", matched_item_id="item-a", **args
        )
        assert first is not None
        assert mirror is None, "the mirrored pair is the same finding"
        assert len(repo.pending("run-A")) == 1

    def test_a_genuinely_different_pair_still_inserts(self, migrated):
        """Negative control for the uniqueness rule."""
        repo = DuplicateRepository(migrated)
        args = {
            "run_id": "run-A",
            "provider_recording_id": "rec-1",
            "fingerprint_digest": "fp-1",
            "score": 0.9,
            "evidence": EVIDENCE,
        }
        repo.insert_acoustid_candidate(candidate_item_id="item-a", matched_item_id="item-b", **args)
        repo.insert_acoustid_candidate(candidate_item_id="item-a", matched_item_id="item-c", **args)
        assert len(repo.pending("run-A")) == 2


class TestP0CreatesPendingOnly:
    def test_a_resolved_status_is_refused(self):
        with pytest.raises(DuplicateContractError) as exc:
            DuplicateCandidate(
                run_id="run-A",
                candidate_item_id="a",
                matched_item_id="b",
                detector=DETECTOR_ACOUSTID,
                evidence=EVIDENCE,
                decision_status="confirmed",
            ).validated()
        assert "pending candidates only" in str(exc.value)

    def test_evidence_is_required(self):
        with pytest.raises(DuplicateContractError) as exc:
            DuplicateCandidate(
                run_id="run-A",
                candidate_item_id="a",
                matched_item_id="b",
                detector=DETECTOR_ACOUSTID,
                evidence={},
            ).validated()
        assert "evidence is required" in str(exc.value)

    def test_evidence_must_record_provenance(self):
        with pytest.raises(DuplicateContractError) as exc:
            DuplicateCandidate(
                run_id="run-A",
                candidate_item_id="a",
                matched_item_id="b",
                detector=DETECTOR_ACOUSTID,
                evidence={"match": "exact"},
            ).validated()
        assert "algorithm and provider" in str(exc.value)

    def test_evidence_may_not_carry_a_credential(self):
        with pytest.raises(DuplicateContractError) as exc:
            DuplicateCandidate(
                run_id="run-A",
                candidate_item_id="a",
                matched_item_id="b",
                detector=DETECTOR_ACOUSTID,
                evidence={**EVIDENCE, "request": {"api_key": "abc123"}},
            ).validated()
        assert "credential" in str(exc.value)

    def test_the_repository_exposes_no_resolution_capability(self):
        """P0 does not resolve, merge, quarantine or delete a candidate."""
        public = [n for n in dir(DuplicateRepository) if not n.startswith("_")]
        forbidden = ("resolve", "merge", "delete", "purge", "confirm", "reject", "apply")
        assert [n for n in public if any(w in n.lower() for w in forbidden)] == []


# ── Migration behaviour ───────────────────────────────────────────────────────


class TestMigrationPreservesLegacyRows:
    def test_legacy_rows_are_kept_not_discarded(self, migrated):
        legacy = migrated.execute("SELECT COUNT(*) FROM duplicates_legacy").fetchone()[0]
        assert legacy == 2, "the legacy table is renamed, never dropped"

    def test_legacy_values_survive_in_a_compatibility_payload(self, migrated):
        import json

        rows = migrated.execute(
            "SELECT evidence_json FROM duplicates WHERE detector LIKE 'legacy_%'"
        ).fetchall()
        assert len(rows) == 2
        payload = json.loads(rows[0]["evidence_json"])
        assert payload["provider"] == "musaeus_legacy_duplicates"
        compat = payload["compatibility"]
        assert compat["group_id"] == "grp-1"
        assert compat["duplicate_type"] == "EXACT"
        assert compat["staged_at"] == "2026-08-01T00:00:00Z"

    def test_a_fresh_database_converges_on_the_same_shape(self, tmp_path):
        """A migration whose result depends on which kind of database it
        met is a migration with two outcomes to reason about."""
        recovery = tmp_path / "recovery2"
        recovery.mkdir()
        fresh = tmp_path / "fresh.db"
        sqlite3.connect(str(fresh)).close()
        migrate(fresh, recovery_root=recovery)

        conn = sqlite3.connect(str(fresh))
        try:
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            columns = [r[1] for r in conn.execute("PRAGMA table_info(duplicates)")]
        finally:
            conn.close()
        assert "duplicates_legacy" in tables
        assert "candidate_item_id" in columns


# ── Transport stays denied ────────────────────────────────────────────────────


class TestNoProviderContact:
    def test_a_fresh_gateways_default_is_local_only(self):
        """Asserted on a NEW Gateway, not on the process-wide one.

        The shared gateway is module-level mutable state and it leaks: a
        test that sets ALLOWED and does not restore it leaves every later
        test in the session running against a permissive gateway. This
        test originally read the global and failed in the full suite while
        passing alone, which is the leak showing itself. Worth fixing at
        the source -- an autouse fixture restoring the policy -- but that
        is network_policy's own repair, not this task's."""
        from musaeus.network_policy import Gateway

        assert Gateway().policy is NetworkPolicy.LOCAL_ONLY

    def test_local_only_denies_and_records_before_raising(self, migrated):
        """P0 keeps AcoustID transport denied. The gateway records the
        attempt before raising so a stage's broad `except Exception` cannot
        erase the evidence -- acousticid._acousticid_lookup has exactly
        such a handler."""
        from musaeus.network_policy import NetworkDenied

        gateway = get_gateway()
        original = gateway.policy
        gateway.policy = NetworkPolicy.LOCAL_ONLY
        gateway.reset()
        try:
            with pytest.raises(NetworkDenied):
                gateway.check("https://api.acoustid.org/v2/lookup")
            assert gateway.denials == ["https://api.acoustid.org/v2/lookup"]

            # And the record survives the swallowing handler the real stage
            # has. contextlib.suppress(Exception) is exactly the
            # `except Exception: pass` that acousticid._acousticid_lookup
            # wraps its call in.
            gateway.reset()
            with contextlib.suppress(Exception):
                gateway.check("https://api.acoustid.org/v2/lookup")
            assert gateway.attempts == ["https://api.acoustid.org/v2/lookup"]
        finally:
            gateway.reset()
            gateway.policy = original

    def test_recording_a_candidate_makes_no_request(self, migrated, transport_harness):
        before = len(transport_harness.attempts)
        DuplicateRepository(migrated).insert_acoustid_candidate(
            run_id="run-A",
            candidate_item_id="item-a",
            matched_item_id="item-b",
            provider_recording_id="rec-1",
            fingerprint_digest="fp-1",
            score=0.9,
            evidence=EVIDENCE,
        )
        assert len(transport_harness.attempts) == before
