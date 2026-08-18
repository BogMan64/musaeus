"""
Tests for tests/disposable_vault.py — the P0-01 fixture harness itself.

This is the "passing isolation test proving the guard blocks real-library
and real-config paths and default transport attempts" required by P0-01's
completion evidence. It deliberately tries to touch real-looking paths and
make a real network call THROUGH the harness and asserts each is rejected.

Also covers: DisposableVault/snapshot_vault_state basic behaviour, and
that config.py's import-time env leak is neutralised for this session
(regression guard for the exact isolation gap found during the P0-01
characterization pass).
"""

from __future__ import annotations

import os
import socket
import sqlite3
from pathlib import Path

import pytest

from tests.disposable_vault import (
    FakeClock,
    NetworkAccessDeniedError,
    RealPathAccessError,
    TransportDenialHarness,
    make_disposable_vault,
    snapshot_vault_state,
)

# ── PathGuard: blocks real-library and real-config paths ─────────────────────


class TestPathGuardBlocksRealPaths:
    """
    The core P0-01 safety proof: deliberately try to touch each protected
    real root through ordinary filesystem/DB operations, through the
    session-wide guard that every other test in this suite already runs
    under, and confirm each attempt is rejected before it can touch disk.
    """

    def test_blocks_mkdir_under_real_music_library(self, path_guard):
        target = Path("/home/grey/Music/__musaeus_p0_01_isolation_probe__")
        assert not target.exists(), (
            "probe path unexpectedly already exists on the real filesystem — "
            "aborting rather than risk touching it"
        )
        with pytest.raises(RealPathAccessError):
            target.mkdir()
        assert not target.exists()

    def test_blocks_write_under_real_musaeus_config(self, path_guard):
        target = Path("/home/grey/.config/musaeus/__musaeus_p0_01_isolation_probe__.txt")
        assert not target.exists()
        with pytest.raises(RealPathAccessError):
            target.write_text("should never be written")
        assert not target.exists()

    def test_blocks_write_under_real_vault(self, path_guard):
        target = Path("/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/__musaeus_p0_01_isolation_probe__.txt")
        assert not target.exists()
        with pytest.raises(RealPathAccessError):
            target.write_text("should never be written")
        assert not target.exists()

    def test_blocks_write_under_real_test_vault(self, path_guard):
        target = Path(
            "/mnt/FORGE2TB/Projects/MUSAEUS_TEST_VAULT/__musaeus_p0_01_isolation_probe__.txt"
        )
        assert not target.exists()
        with pytest.raises(RealPathAccessError):
            target.write_text("should never be written")
        assert not target.exists()

    def test_blocks_mkdir_under_future_recovery_root(self, path_guard):
        """
        /home/grey/Projects/MUSAEUS_RECOVERY is the fixed FUTURE recovery
        root per both requirements.md and design.md: "no task creates or
        probes that directory". It must not exist on disk at all right
        now — this test both proves the guard blocks it AND documents
        that expectation as an executable assertion, not just prose.
        """
        real_recovery_root = Path("/home/grey/Projects/MUSAEUS_RECOVERY")
        assert not real_recovery_root.exists(), (
            "the future-only recovery root must never be created; if this "
            "assertion fails, something in this repo violated that policy "
            "boundary — this is the correct place to notice"
        )
        with pytest.raises(RealPathAccessError):
            real_recovery_root.mkdir()
        assert not real_recovery_root.exists()

    def test_blocks_sqlite_connect_to_real_vault_db(self, path_guard):
        real_db = "/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/musaeus.db"
        with pytest.raises(RealPathAccessError):
            sqlite3.connect(real_db)

    def test_records_attempts(self, path_guard):
        path_guard.reset_attempts()
        target = Path("/home/grey/Music/__another_probe__")
        with pytest.raises(RealPathAccessError):
            target.mkdir()
        assert len(path_guard.attempts) == 1
        event, path_str, matched_root = path_guard.attempts[0]
        assert event == "os.mkdir"
        assert path_str == str(target)
        assert matched_root == "/home/grey/Music"

    def test_disposable_vault_paths_are_unaffected(self, disposable_vault, path_guard):
        """The guard must not false-positive on ordinary disposable-vault
        operations — only PROTECTED_REAL_ROOTS are blocked."""
        disposable_vault.cfg.ensure_dirs()
        assert disposable_vault.root.exists()
        (disposable_vault.cfg.inbox / "probe.flac").write_bytes(b"not real audio")
        assert (disposable_vault.cfg.inbox / "probe.flac").exists()
        conn = disposable_vault.open_db()
        conn.close()
        assert disposable_vault.cfg.db_path.exists()


# ── TransportDenialHarness: blocks default network access ────────────────────


class TestTransportDenialHarness:
    """
    Proves the harness rejects an outbound network attempt through both a
    raw socket.connect() call and through urllib (the mechanism every
    real network-touching stage in this repo — enrich.py, mb_enrich.py,
    acousticid.py, reviewer.py — actually uses).
    """

    def test_session_harness_blocks_raw_socket_connect(self, transport_harness):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(NetworkAccessDeniedError):
                s.connect(("93.184.216.34", 80))  # example.com's IP literal, no DNS needed
        finally:
            s.close()

    def test_session_harness_blocks_urlopen(self, transport_harness):
        import urllib.request

        with pytest.raises(Exception) as exc_info:
            urllib.request.urlopen("http://example.com/", timeout=2)
        # urlopen wraps the underlying connect() failure in a URLError;
        # confirm our specific denial is what actually caused it.
        assert "blocked outbound network connection" in str(exc_info.value)

    def test_standalone_harness_records_and_restores(self):
        """A fresh, non-session harness installs, blocks, records, and
        cleanly restores the original socket.socket.connect on exit."""
        harness = TransportDenialHarness()
        original_connect = socket.socket.connect

        with harness.active():
            assert socket.socket.connect is not original_connect
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                with pytest.raises(NetworkAccessDeniedError):
                    s.connect(("93.184.216.34", 443))
            finally:
                s.close()

        assert socket.socket.connect is original_connect
        assert harness.attempts == [("connect", ("93.184.216.34", 443))]

    def test_mb_enrich_validate_no_longer_touches_the_network(
        self, disposable_vault, transport_harness
    ):
        """
        2026-08-17: MBEnrichStage joined DEFAULT_PIPELINE's default-on
        chain, so validate() hard-failing the whole run over a network
        hiccup was no longer acceptable -- the connectivity check moved
        into _enrich() itself, where it can degrade gracefully (skip +
        report, matching EnrichStage's missing-API-key pattern) instead
        of raising StageError. validate() itself must now complete
        cleanly with zero network attempts, even under the session-wide
        transport harness.
        """
        from musaeus.stages.mb_enrich import MBEnrichStage

        conn = disposable_vault.open_db()
        ctx = disposable_vault.new_context(conn, dry_run=False)
        attempts_before = len(transport_harness.attempts)

        MBEnrichStage().validate(ctx)  # must not raise

        assert len(transport_harness.attempts) == attempts_before
        conn.close()


# ── Regression guard: the import-time env leak this harness closes ───────────


class TestEnvLeakClosed:
    """
    Regression test for the exact isolation gap found during the P0-01
    characterization pass: musaeus.config's module-level _load_env() call
    injects real values from ~/.config/musaeus/settings.env and
    credentials.env into os.environ at import time. tests/conftest.py
    redirects HOME (and clears the specific MUSAEUS_*/API key vars)
    before musaeus.config can ever be imported in this process, so this
    should never be the real vault path.
    """

    def test_musaeus_vault_root_env_is_not_the_real_vault(self):
        assert os.environ.get("MUSAEUS_VAULT_ROOT") != "/mnt/FORGE2TB/Projects/MUSAEUS_VAULT"

    def test_config_module_user_config_dir_is_not_real_home(self):
        import musaeus.config as config_mod

        assert str(config_mod._USER_CONFIG_DIR) != "/home/grey/.config/musaeus"

    def test_cli_resume_file_is_not_real_home(self):
        import musaeus.cli as cli_mod

        assert "/home/grey" not in str(cli_mod._RESUME_FILE)

    def test_wizard_settings_files_are_not_real_home(self):
        import musaeus.setup.wizard as wizard_mod

        assert "/home/grey" not in str(wizard_mod._SETTINGS_FILE)
        assert "/home/grey" not in str(wizard_mod._CREDENTIALS_FILE)

    def test_get_config_singleton_does_not_resolve_to_real_vault(self):
        """
        NearDupeStage/EnrichStage call get_config() directly (a
        process-wide singleton) instead of using ctx.config — confirmed
        during characterization as a real (if currently benign, since
        existing tests mock it) isolation gap. With the env leak closed,
        the singleton itself can no longer resolve to the real vault
        even for code that bypasses ctx.config.
        """
        import musaeus.config as config_mod

        # get_config() is a cached singleton; only assert about the
        # values a fresh from_env() would produce with no MUSAEUS_VAULT_ROOT
        # set, to avoid this test's result depending on whichever test
        # ran first and happened to populate the cache.
        with pytest.raises(ValueError, match="MUSAEUS_VAULT_ROOT is not set"):
            config_mod.MusicConfig.from_env()


# ── DisposableVault / snapshot_vault_state basics ─────────────────────────────


class TestDisposableVaultBasics:
    def test_vault_paths_are_under_tmp_path(self, disposable_vault, tmp_path):
        assert disposable_vault.root == tmp_path / "vault"
        assert str(disposable_vault.root).startswith(str(tmp_path))
        assert str(disposable_vault.config_home).startswith(str(tmp_path))
        assert str(disposable_vault.recovery_root).startswith(str(tmp_path))
        assert str(disposable_vault.report_root).startswith(str(tmp_path))

    def test_open_db_and_new_context(self, disposable_vault):
        disposable_vault.cfg.ensure_dirs()
        conn = disposable_vault.open_db()
        ctx = disposable_vault.new_context(conn, dry_run=True)
        assert ctx.dry_run is True
        assert ctx.config is disposable_vault.cfg
        ctx.finish()

    def test_two_vaults_from_same_tmp_path_are_independent(self, tmp_path):
        v1 = make_disposable_vault(tmp_path, name="vault_a")
        v2 = make_disposable_vault(tmp_path, name="vault_b")
        assert v1.root != v2.root
        v1.cfg.ensure_dirs()
        assert not v2.root.exists()


class TestSnapshotVaultState:
    def test_snapshot_before_after_equal_when_nothing_changes(self, disposable_vault):
        disposable_vault.cfg.ensure_dirs()
        before = snapshot_vault_state(disposable_vault)
        after = snapshot_vault_state(disposable_vault)
        assert before.equals(after)
        assert before.diff(after) == []

    def test_snapshot_detects_new_file(self, disposable_vault):
        disposable_vault.cfg.ensure_dirs()
        before = snapshot_vault_state(disposable_vault)
        (disposable_vault.cfg.inbox / "new_track.flac").write_bytes(b"content")
        after = snapshot_vault_state(disposable_vault)
        assert not before.equals(after)
        diffs = before.diff(after)
        assert any("added" in d for d in diffs)

    def test_snapshot_detects_db_event_count_change(self, disposable_vault):
        disposable_vault.cfg.ensure_dirs()
        conn = disposable_vault.open_db()
        before = snapshot_vault_state(disposable_vault)

        ctx = disposable_vault.new_context(conn, dry_run=False)
        ctx.finish()

        after = snapshot_vault_state(disposable_vault)
        assert not before.equals(after)
        assert any("event count" in d for d in before.diff(after))

    def test_snapshot_no_db_is_handled(self, disposable_vault):
        disposable_vault.cfg.ensure_dirs()
        snap = snapshot_vault_state(disposable_vault)
        assert snap.db_exists is False
        assert snap.db_content_checksum is None
        assert snap.db_event_count == 0


# ── FakeClock ──────────────────────────────────────────────────────────────────


class TestFakeClock:
    def test_advance_seconds(self):
        clock = FakeClock()
        t0 = clock.now()
        clock.advance(seconds=30)
        assert (clock.now() - t0).total_seconds() == 30

    def test_freeze(self):
        from datetime import datetime, timezone

        clock = FakeClock()
        target = datetime(2030, 6, 15, tzinfo=timezone.utc)
        clock.freeze(target)
        assert clock.now() == target

    def test_utcnow_iso_format(self):
        clock = FakeClock()
        iso = clock.utcnow_iso()
        assert "2026-01-01" in iso
