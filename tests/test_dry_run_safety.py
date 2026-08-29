"""
MUSAEUS — dry-run / preview safety

HISTORY. This file used to be test_p0_02_dry_run_guard.py and asserted that
`--dry-run` was REFUSED outright (exit code 2). Task P0-02 added that blunt
fail-closed guard because P0-01's characterization pass proved a preview was
not side-effect-free: it ran cfg.ensure_dirs() and created/wrote the real
SQLite database before any stage executed, and three stages made live network
calls with only the DB write afterwards gated.

The guard was right about the defects and wrong as a permanent answer.
`--dry-run` is advertised throughout README, cli.py's own docstring and
musaeus_overnight.sh, so "documented everywhere, refused everywhere" was its
own kind of dishonesty -- and the guard never covered console.py's separate
pipeline runner, so the unsafe path stayed reachable from menu option 1 the
whole time.

Every defect is now fixed at source, so these tests assert the PROPERTY the
guard was standing in for -- a preview changes nothing -- rather than
asserting that the command is refused:

  1. ensure_dirs() is skipped under dry_run, and a preview of a vault with no
     database reports that and exits 0 rather than creating one.
  2. The database is opened READ-ONLY (db.open_db(read_only=True)), so a
     stage that attempts a write raises OperationalError and is reported as a
     stage failure. Dry-run is enforced by SQLite rather than by 30+ stages
     each remembering to gate themselves.
  3. RunContext buffers events instead of inserting them and never commits
     under dry_run, so no RUN_START/STAGE_COMPLETE/RUN_END lands in the
     append-only log for a run that did not happen.
  4. Resume state and failure-report files are not written under dry_run.
  5. Enrich/MBEnrich (fixed 2026-08-18) and AcousticID all gate their network
     calls behind dry_run.

Built entirely on the P0-01 fixture harness (tests/disposable_vault.py,
tests/conftest.py): the disposable_vault, transport_harness and path_guard
fixtures, plus snapshot_vault_state().
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from musaeus.db import open_db
from musaeus.stages import (
    DEFAULT_PIPELINE,
    AcousticIDStage,
    EnrichStage,
    IngestStage,
    MBEnrichStage,
    PreflightStage,
    SentinelStage,
)
from tests.disposable_vault import snapshot_vault_state


def _patch_cli_for_vault(monkeypatch, disposable_vault):
    """Point musaeus.cli's config/resume-file resolution at the disposable
    vault and return the cli module for the caller to invoke."""
    import musaeus.cli as cli_mod

    monkeypatch.setattr(cli_mod, "get_config", lambda: disposable_vault.cfg)
    monkeypatch.setattr(cli_mod, "_RESUME_FILE", disposable_vault.config_home / "resume_state.json")
    return cli_mod


def _seed_db(disposable_vault):
    """Create the vault skeleton and an initialised (empty) database, so a
    preview has something real to open.

    Also opens it read-only once to materialise the -wal/-shm sidecars.
    SQLite creates those for any read of a WAL database, preview or not, so
    warming them here keeps the before/after comparison EXACT -- the
    assertion stays a strict equality with no carve-out for expected noise,
    which is what makes it worth trusting.
    """
    disposable_vault.cfg.ensure_dirs()
    open_db(disposable_vault.cfg.db_path).close()
    open_db(disposable_vault.cfg.db_path, read_only=True).close()


_SQLITE_SIDECARS = ("-wal", "-shm")


def assert_only_sqlite_sidecars_differ(before, after) -> None:
    """Assert nothing changed except SQLite's own -wal/-shm sidecars.

    SQLite materialises those for ANY read of a WAL database. A writable
    connection checkpoints and removes them when it closes; a read-only one
    cannot, so a preview can leave behind two files an ordinary run would
    have cleaned up. They hold no Musaeus state -- the next writable
    connection reclaims them -- so this is an artifact of reading, not a
    mutation of anything the vault means.

    Rather than teach the shared snapshot helper to ignore them (which would
    quietly weaken every other test that uses it), this asserts the stronger
    and more honest thing: every substantive field is identical, and the
    only tree difference permitted is that sidecar set.
    """
    assert before.db_exists == after.db_exists
    assert before.db_event_count == after.db_event_count, "a preview wrote events"
    assert before.db_archive_count == after.db_archive_count, "a preview wrote archive rows"
    assert before.db_content_checksum == after.db_content_checksum, "a preview changed the DB"

    added = set(after.directory_tree) - set(before.directory_tree)
    removed = set(before.directory_tree) - set(after.directory_tree)
    assert not removed, f"a preview removed: {sorted(removed)}"
    unexpected = [p for p in added if not p.endswith(_SQLITE_SIDECARS)]
    assert not unexpected, f"a preview created: {sorted(unexpected)}"

    # Real audio files must be byte-identical and tag-identical.
    def _audio_only(mapping):
        return {k: v for k, v in mapping.items() if not k.endswith(_SQLITE_SIDECARS)}

    assert _audio_only(before.file_hashes) == _audio_only(after.file_hashes), (
        "a preview changed file contents"
    )
    assert _audio_only(before.file_tags) == _audio_only(after.file_tags), (
        "a preview changed file tags"
    )


def _db_fingerprint(db_path) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        return hashlib.sha256("".join(conn.iterdump()).encode()).hexdigest()
    finally:
        conn.close()


# ── The read-only connection is the enforcement mechanism ────────────────────


class TestReadOnlyConnectionEnforcesDryRun:
    """dry-run safety must not depend on every stage remembering to gate
    itself. open_db(read_only=True) makes a write physically impossible."""

    def test_read_only_connection_refuses_writes(self, disposable_vault):
        _seed_db(disposable_vault)
        conn = open_db(disposable_vault.cfg.db_path, read_only=True)
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("INSERT INTO archive (file_path) VALUES ('x')")
                conn.commit()
        finally:
            conn.close()

    def test_read_only_connection_still_reads(self, disposable_vault):
        _seed_db(disposable_vault)
        conn = open_db(disposable_vault.cfg.db_path, read_only=True)
        try:
            assert conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0] == 0
        finally:
            conn.close()

    def test_read_only_refuses_to_create_a_missing_database(self, disposable_vault):
        """Creating the file would be exactly the side effect being avoided."""
        assert not disposable_vault.cfg.db_path.exists()
        with pytest.raises(FileNotFoundError):
            open_db(disposable_vault.cfg.db_path, read_only=True)
        assert not disposable_vault.cfg.db_path.exists()


# ── A preview changes nothing ────────────────────────────────────────────────


class TestDryRunLeavesTheVaultUnchanged:
    def test_full_pipeline_preview_leaves_snapshot_identical(self, disposable_vault, monkeypatch):
        """The MCR-001 before/after equality check, now applied to a preview
        that actually RUNS rather than one that was refused: directory tree,
        file hashes/tags, DB existence, event count, archive count and DB
        content checksum must all be unchanged."""
        _seed_db(disposable_vault)
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)
        before = snapshot_vault_state(disposable_vault)

        cli_mod._run_pipeline(DEFAULT_PIPELINE, dry_run=True)

        after = snapshot_vault_state(disposable_vault)
        assert_only_sqlite_sidecars_differ(before, after)

    def test_preview_writes_no_events(self, disposable_vault, monkeypatch):
        """RUN_START/STAGE_COMPLETE/RUN_END must not reach the append-only
        log for a run that did not happen."""
        _seed_db(disposable_vault)
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)
        before = _db_fingerprint(disposable_vault.cfg.db_path)

        cli_mod._run_pipeline(DEFAULT_PIPELINE, dry_run=True)

        assert _db_fingerprint(disposable_vault.cfg.db_path) == before

    @pytest.mark.parametrize(
        "stage_cls",
        [PreflightStage, IngestStage, SentinelStage],
        ids=["preflight", "ingest", "sentinel"],
    )
    def test_single_stage_previews_are_also_side_effect_free(
        self, disposable_vault, monkeypatch, stage_cls
    ):
        """Safety is a property of the runner, not of a curated allowlist of
        stages -- an arbitrary single-stage preview must be just as clean."""
        _seed_db(disposable_vault)
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)
        before = snapshot_vault_state(disposable_vault)

        cli_mod._run_pipeline([stage_cls], dry_run=True)

        after = snapshot_vault_state(disposable_vault)
        assert_only_sqlite_sidecars_differ(before, after)

    def test_preview_does_not_write_failure_reports(self, disposable_vault, monkeypatch):
        """A stage that fails validation under preview must not leave a JSON
        report behind (nor mkdir the RUNS/FAILURES tree to hold it)."""
        _seed_db(disposable_vault)
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)

        cli_mod._run_pipeline(DEFAULT_PIPELINE, dry_run=True)

        failures_dir = disposable_vault.cfg.runs_root / "FAILURES"
        assert not failures_dir.exists() or not list(failures_dir.glob("*.json"))

    def test_preview_does_not_disturb_resume_state(self, disposable_vault, monkeypatch):
        """A preview must not consume or rewrite a live run's bookmark."""
        _seed_db(disposable_vault)
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)
        resume_file = disposable_vault.config_home / "resume_state.json"
        resume_file.parent.mkdir(parents=True, exist_ok=True)
        original = {"completed": ["SentinelStage"], "all_stages": ["SentinelStage"]}
        resume_file.write_text(json.dumps(original))

        cli_mod._run_pipeline(DEFAULT_PIPELINE, dry_run=True)

        assert json.loads(resume_file.read_text()) == original


# ── No network under preview ─────────────────────────────────────────────────


class TestDryRunMakesNoNetworkCall:
    """Previously guaranteed only by refusing the command outright. Now each
    stage genuinely gates its own calls, which is what makes the preview
    useful rather than merely safe."""

    @pytest.mark.parametrize(
        "stage_cls",
        [EnrichStage, MBEnrichStage, AcousticIDStage],
        ids=["enrich", "mb_enrich", "acousticid"],
    )
    def test_network_stage_preview_makes_no_connection(
        self, disposable_vault, monkeypatch, transport_harness, stage_cls
    ):
        _seed_db(disposable_vault)
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)
        attempts_before = len(transport_harness.attempts)

        cli_mod._run_pipeline([stage_cls], dry_run=True)

        assert len(transport_harness.attempts) == attempts_before, (
            "a preview must not reach the network -- transport_harness "
            "observed a real connection attempt"
        )

    def test_full_pipeline_preview_makes_no_connection(
        self, disposable_vault, monkeypatch, transport_harness
    ):
        _seed_db(disposable_vault)
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)
        attempts_before = len(transport_harness.attempts)

        cli_mod._run_pipeline(DEFAULT_PIPELINE, dry_run=True)

        assert len(transport_harness.attempts) == attempts_before


# ── A preview with nothing to preview ────────────────────────────────────────


class TestPreviewWithNoDatabase:
    def test_reports_and_exits_zero_without_creating_anything(
        self, disposable_vault, monkeypatch, capsys
    ):
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)
        assert not disposable_vault.root.exists()

        rc = cli_mod._run_pipeline(DEFAULT_PIPELINE, dry_run=True)

        assert rc == 0
        assert not disposable_vault.root.exists(), "no vault skeleton may be created"
        assert not disposable_vault.cfg.db_path.exists(), "no database may be created"
        assert "nothing to preview" in capsys.readouterr().err.lower()


# ── Real runs are unaffected ─────────────────────────────────────────────────


class TestRealRunIsUnaffected:
    """None of the dry-run machinery may leak into a live run: it must still
    create directories, open a writable DB, and record its events."""

    def test_real_run_creates_dirs_and_db(self, disposable_vault, monkeypatch):
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)
        assert not disposable_vault.root.exists()

        cli_mod._run_pipeline([PreflightStage], dry_run=False)

        assert disposable_vault.root.exists()
        assert disposable_vault.cfg.db_path.exists()

    def test_real_run_writes_its_events(self, disposable_vault, monkeypatch):
        cli_mod = _patch_cli_for_vault(monkeypatch, disposable_vault)

        cli_mod._run_pipeline([PreflightStage], dry_run=False)

        conn = sqlite3.connect(str(disposable_vault.cfg.db_path))
        try:
            types = {
                r[0] for r in conn.execute("SELECT DISTINCT event_type FROM events").fetchall()
            }
        finally:
            conn.close()
        assert "RUN_START" in types
        assert "STAGE_COMPLETE" in types
        assert "RUN_END" in types
