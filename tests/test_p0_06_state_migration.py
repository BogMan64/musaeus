"""
P0-06 — versioned state schema, migration ledger, backup, candidate swap.

Every test here measures an effect on a real disposable database: a
version actually stamped, a ledger row actually written, bytes actually
restored, a file actually left alone. None of them assert that a function
was called.

That is not stylistic. `MUSAEUS_OPEN_ITEMS.md` now lists eight components
that did nothing while reporting success, and the shared shape of all
eight was an assertion describing the shape of a call rather than an
effect on disk. Several tests below therefore carry a *negative control*:
first prove the guard fires when it should, then prove the same code path
succeeds when it should not fire. A guard that is never observed to do
both is indistinguishable from a guard that always says the same thing.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from musaeus.state import migrator as migrator_mod
from musaeus.state import policy
from musaeus.state.migrations import (
    MIGRATIONS,
    Migration,
    MigrationRegistryError,
    plan_migrations,
    validate_registry,
)
from musaeus.state.migrator import (
    BackupVerificationError,
    MigrationFailedError,
    RecoveryTargetError,
    create_verified_backup,
    migrate,
    restore_from_backup,
)
from musaeus.state.schema import (
    LEGACY_UNVERSIONED,
    OUTCOME_FAILED,
    OUTCOME_RUNNING,
    OUTCOME_SUCCEEDED,
    SCHEMA_VERSION,
    DatabaseReadOnlyError,
    MigrationLedgerError,
    SchemaIncompatibleError,
    check_compatibility,
    detect_read_only,
    read_schema_version,
    read_state_metadata,
    table_exists,
)
from tests.disposable_vault import snapshot_vault_state

# ── Local helpers ─────────────────────────────────────────────────────────────


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _make_legacy_db(vault) -> Path:
    """A database exactly as the shipping `db.open_db()` produces it: real
    tables, real rows, and no version stamp anywhere."""
    conn = vault.open_db()
    try:
        conn.execute(
            "INSERT INTO events (run_id, event_type, file_path, stage) VALUES (?, ?, ?, ?)",
            ("run-legacy", "INGESTED", "/x/a.m4a", "IngestStage"),
        )
        conn.execute(
            "INSERT INTO archive (file_path, artist, title, status) VALUES (?, ?, ?, ?)",
            ("/x/a.m4a", "Bob Seger", "Night Moves", "CATALOGUED"),
        )
        conn.commit()
    finally:
        conn.close()
    return vault.cfg.db_path


def _ledger_rows(db_path: Path) -> list[sqlite3.Row]:
    conn = _connect(db_path)
    try:
        return conn.execute("SELECT * FROM schema_migrations ORDER BY migration_id").fetchall()
    finally:
        conn.close()


def _backup_files(recovery_root: Path) -> list[Path]:
    return sorted(recovery_root.glob("*.db"))


# ── Registry declaration ──────────────────────────────────────────────────────


class TestRegistryDeclaration:
    def test_shipped_registry_is_valid(self):
        validate_registry()

    def test_chain_reaches_declared_schema_version(self):
        """The check that keeps SCHEMA_VERSION honest: bumping the constant
        without adding the migration that reaches it must fail loudly."""
        assert MIGRATIONS[-1].to_version == SCHEMA_VERSION
        assert MIGRATIONS[0].from_version == LEGACY_UNVERSIONED

    def test_checksum_is_derived_from_sql_not_identity(self):
        base = MIGRATIONS[0]
        same = Migration(
            migration_id=base.migration_id,
            from_version=base.from_version,
            to_version=base.to_version,
            statements=base.statements,
            description="a totally different description",
        )
        changed = Migration(
            migration_id=base.migration_id,
            from_version=base.from_version,
            to_version=base.to_version,
            statements=(*base.statements, "CREATE TABLE extra (id INTEGER)"),
            description=base.description,
        )
        assert same.checksum == base.checksum, "description must not affect the checksum"
        assert changed.checksum != base.checksum, "changed SQL must change the checksum"

    @pytest.mark.parametrize(
        "bad, expect",
        [
            ((), "empty"),
            (
                (
                    Migration("0001_a", 0, 1, ("SELECT 1",), ""),
                    Migration("0001_a", 1, 2, ("SELECT 1",), ""),
                ),
                "duplicate",
            ),
            ((Migration("0001_a", 1, 1, ("SELECT 1",), ""),), "monotonic"),
            ((Migration("0001_a", 2, 1, ("SELECT 1",), ""),), "monotonic"),
            (
                (
                    Migration("0001_a", 0, 1, ("SELECT 1",), ""),
                    Migration("0002_b", 5, 6, ("SELECT 1",), ""),
                ),
                "gap",
            ),
            (
                (
                    Migration("0002_b", 0, 1, ("SELECT 1",), ""),
                    Migration("0001_a", 1, 2, ("SELECT 1",), ""),
                ),
                "sort",
            ),
        ],
    )
    def test_invalid_chains_are_rejected(self, bad, expect):
        with pytest.raises(MigrationRegistryError) as exc:
            validate_registry(bad)
        assert expect in str(exc.value).lower()

    def test_version_between_declared_migrations_is_refused(self):
        """A database at a version no migration declares is a disagreement,
        not something to round down to the nearest earlier migration."""
        chain = (
            Migration("0001_a", 0, 1, ("SELECT 1",), ""),
            Migration("0002_b", 1, 4, ("SELECT 1",), ""),
        )
        with pytest.raises(MigrationRegistryError) as exc:
            plan_migrations(2, chain)
        assert "between declared versions" in str(exc.value)

    def test_plan_is_empty_when_already_current(self):
        assert plan_migrations(SCHEMA_VERSION) == ()


# ── Policy values ─────────────────────────────────────────────────────────────


class TestRecoveryPolicy:
    def test_fixed_values_are_exact(self):
        assert policy.FUTURE_RECOVERY_ROOT == "/home/grey/Projects/MUSAEUS_RECOVERY"
        assert policy.RECOVERY_CAP_BYTES == 100 * 10**9

    def test_cap_is_decimal_gb_not_gib(self):
        """100 GiB would be 7.4% more headroom than the figure Grey fixed,
        in the permissive direction."""
        assert policy.RECOVERY_CAP_BYTES != 100 * 2**30
        assert policy.RECOVERY_CAP_BYTES < 100 * 2**30

    def test_describe_returns_a_fresh_mapping(self):
        first = policy.describe_recovery_policy()
        first["recovery_cap_bytes"] = 1
        assert policy.describe_recovery_policy()["recovery_cap_bytes"] == policy.RECOVERY_CAP_BYTES

    def test_declared_policy_never_probes_the_future_root(self, path_guard):
        """The session PathGuard lists the future recovery root in
        PROTECTED_REAL_ROOTS, so any stat/open of it raises. Reading the
        policy must not trip it."""
        before = len(path_guard.attempts)
        policy.describe_recovery_policy()
        assert len(path_guard.attempts) == before

    def test_future_root_is_refused_as_a_recovery_target(self, disposable_vault, path_guard):
        db = _make_legacy_db(disposable_vault)
        before = len(path_guard.attempts)
        with pytest.raises(RecoveryTargetError) as exc:
            migrate(db, recovery_root=Path(policy.FUTURE_RECOVERY_ROOT))
        assert "fixed future recovery root" in str(exc.value)
        # Refused by string comparison alone -- no stat(), no readlink().
        assert len(path_guard.attempts) == before

    def test_future_root_subdirectory_is_refused_too(self, disposable_vault):
        db = _make_legacy_db(disposable_vault)
        with pytest.raises(RecoveryTargetError):
            migrate(db, recovery_root=Path(policy.FUTURE_RECOVERY_ROOT) / "nested")


# ── Fresh and legacy migration ────────────────────────────────────────────────


class TestMigrateFreshDatabase:
    def test_empty_database_reaches_current_version_with_ledger(self, disposable_vault, tmp_path):
        db = tmp_path / "fresh" / "musaeus.db"
        db.parent.mkdir(parents=True)
        _connect(db).close()

        result = migrate(db, recovery_root=disposable_vault.recovery_root)

        assert result.was_empty_database is True
        assert result.from_version == LEGACY_UNVERSIONED
        assert result.to_version == SCHEMA_VERSION
        assert result.applied == tuple(m.migration_id for m in MIGRATIONS)

        conn = _connect(db)
        try:
            assert read_schema_version(conn) == SCHEMA_VERSION
            meta = read_state_metadata(conn)
        finally:
            conn.close()
        assert meta is not None
        assert meta.schema_version == SCHEMA_VERSION
        assert meta.app_max_schema == SCHEMA_VERSION
        assert meta.created_at == meta.updated_at

        rows = _ledger_rows(db)
        assert [r["migration_id"] for r in rows] == list(result.applied)
        for row, declared in zip(rows, MIGRATIONS, strict=True):
            assert row["outcome"] == OUTCOME_SUCCEEDED
            assert row["finished_at"] is not None
            assert row["checksum"] == declared.checksum
            assert row["from_version"] == declared.from_version
            assert row["to_version"] == declared.to_version
            assert row["backup_ref"] is not None and "sha256=" in row["backup_ref"]


class TestMigrateLegacyDatabase:
    def test_legacy_rows_survive_and_version_is_stamped(self, disposable_vault):
        db = _make_legacy_db(disposable_vault)

        conn = _connect(db)
        try:
            assert read_schema_version(conn) == LEGACY_UNVERSIONED
            assert table_exists(conn, "state_metadata") is False
        finally:
            conn.close()

        result = migrate(db, recovery_root=disposable_vault.recovery_root)
        assert result.was_empty_database is False
        assert result.from_version == LEGACY_UNVERSIONED
        assert result.to_version == SCHEMA_VERSION

        conn = _connect(db)
        try:
            assert read_schema_version(conn) == SCHEMA_VERSION
            assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
            row = conn.execute("SELECT artist, title FROM archive").fetchone()
            assert (row["artist"], row["title"]) == ("Bob Seger", "Night Moves")
        finally:
            conn.close()

    def test_migration_touches_no_library_content(self, disposable_vault):
        """MCR-004's 'no path to content mutation': the only thing the
        migrator may change is the database."""
        lib = disposable_vault.cfg.alac_library / "Bob Seger"
        lib.mkdir(parents=True)
        track = lib / "Night Moves.m4a"
        track.write_bytes(b"FAKE ALAC PAYLOAD")
        db = _make_legacy_db(disposable_vault)

        before = snapshot_vault_state(disposable_vault)
        migrate(db, recovery_root=disposable_vault.recovery_root)
        after = snapshot_vault_state(disposable_vault)

        db_names = {disposable_vault.cfg.db_path.name}
        before_files = {k: v for k, v in before.file_hashes.items() if k not in db_names}
        after_files = {k: v for k, v in after.file_hashes.items() if k not in db_names}
        assert before_files == after_files
        assert (
            after_files[f"ALAC-Library/Bob Seger/{track.name}"]
            == before_files[f"ALAC-Library/Bob Seger/{track.name}"]
        )
        assert before.db_event_count == after.db_event_count == 1
        assert before.db_archive_count == after.db_archive_count == 1

    def test_second_migrate_is_a_true_no_op(self, disposable_vault):
        """Not 'returns without error' -- changes nothing measurable and
        creates no backup. This path will eventually run on every open, so
        a no-op that still writes would be a slow leak."""
        db = _make_legacy_db(disposable_vault)
        migrate(db, recovery_root=disposable_vault.recovery_root)

        backups_after_first = _backup_files(disposable_vault.recovery_root)
        assert len(backups_after_first) == 1
        before = snapshot_vault_state(disposable_vault)

        result = migrate(db, recovery_root=disposable_vault.recovery_root)

        assert result.applied == ()
        assert result.changed is False
        assert result.backup is None
        assert _backup_files(disposable_vault.recovery_root) == backups_after_first

        after = snapshot_vault_state(disposable_vault)

        # Logical database content: identical.
        assert before.db_content_checksum == after.db_content_checksum
        assert before.db_event_count == after.db_event_count
        assert before.db_archive_count == after.db_archive_count

        # Filesystem: identical except SQLite's WAL sidecars, which the
        # test names rather than waves at. Opening a WAL database
        # *read-only* creates `-wal`/`-shm` and cannot remove them on close
        # (deleting them needs a write lock this connection does not hold)
        # -- verified directly, not assumed. They are SQLite's reader
        # index, recreated by any reader including `sqlite3 db "SELECT 1"`;
        # counting them as a state change would mean reading a database
        # modifies it, which would make every read-only preflight check in
        # P0-11 impossible to describe. The assertion is an equality on the
        # exact set, so anything *else* appearing still fails here.
        sidecars = {f"{db.name}-wal", f"{db.name}-shm"}
        new_paths = set(after.directory_tree) - set(before.directory_tree)
        assert new_paths == sidecars, f"unexpected new paths: {sorted(new_paths - sidecars)}"
        assert set(before.directory_tree) - set(after.directory_tree) == set()

        persistent_before = {k: v for k, v in before.file_hashes.items() if k not in sidecars}
        persistent_after = {k: v for k, v in after.file_hashes.items() if k not in sidecars}
        assert persistent_before == persistent_after

    def test_inspection_opens_the_database_read_only(self, disposable_vault):
        """Proven by effect, not by reading the connection string: the
        inspection connection is handed a database whose permissions allow
        reads only. The read succeeds and a write raises."""
        db = _make_legacy_db(disposable_vault)
        migrate(db, recovery_root=disposable_vault.recovery_root)

        conn = _connect(db)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

        original = stat.S_IMODE(db.stat().st_mode)
        db.chmod(0o444)
        try:
            if os.access(db, os.W_OK):
                pytest.skip("running with write override (root); permission bits not enforced")
            inspect = migrator_mod._connect_readonly(db)
            try:
                assert read_schema_version(inspect) == SCHEMA_VERSION
                with pytest.raises(sqlite3.OperationalError):
                    inspect.execute("CREATE TABLE should_not_exist (x INTEGER)")
            finally:
                inspect.close()
        finally:
            db.chmod(original)


# ── Compatibility and fail-closed guards ──────────────────────────────────────


class TestForwardVersionIsRefused:
    def test_check_compatibility_rejects_newer_and_accepts_supported(self):
        with pytest.raises(SchemaIncompatibleError) as exc:
            check_compatibility(SCHEMA_VERSION + 1)
        assert exc.value.reason_code == "schema_incompatible"
        assert exc.value.details["found_version"] == SCHEMA_VERSION + 1
        # Negative control: the same function must not reject what it supports.
        check_compatibility(SCHEMA_VERSION)
        check_compatibility(LEGACY_UNVERSIONED)

    def test_newer_database_blocks_before_any_write(self, disposable_vault):
        db = _make_legacy_db(disposable_vault)
        migrate(db, recovery_root=disposable_vault.recovery_root)
        conn = _connect(db)
        try:
            conn.execute("UPDATE state_metadata SET schema_version = 999 WHERE id = 1")
        finally:
            conn.close()

        for stale in _backup_files(disposable_vault.recovery_root):
            stale.unlink()
        before = snapshot_vault_state(disposable_vault)

        with pytest.raises(SchemaIncompatibleError):
            migrate(db, recovery_root=disposable_vault.recovery_root)

        after = snapshot_vault_state(disposable_vault)
        assert before.db_content_checksum == after.db_content_checksum
        assert _backup_files(disposable_vault.recovery_root) == [], (
            "a refusal must leave no trace -- not even a backup file"
        )


class TestReadOnlyDatabaseIsRefused:
    def test_unwritable_database_file_blocks(self, disposable_vault):
        db = _make_legacy_db(disposable_vault)
        original = stat.S_IMODE(db.stat().st_mode)
        db.chmod(0o444)
        try:
            if os.access(db, os.W_OK):
                pytest.skip("running with write override (root); permission bits not enforced")
            assert detect_read_only(db) is True
            with pytest.raises(DatabaseReadOnlyError) as exc:
                migrate(db, recovery_root=disposable_vault.recovery_root)
            assert exc.value.reason_code == "database_read_only"
        finally:
            db.chmod(original)

    def test_writable_database_is_not_flagged(self, disposable_vault):
        """Negative control for the check above: it must be capable of
        returning False, or it is not a check."""
        db = _make_legacy_db(disposable_vault)
        assert detect_read_only(db) is False


class TestUnfinishedAttemptBlocks:
    def test_running_ledger_row_fails_closed(self, disposable_vault):
        db = _make_legacy_db(disposable_vault)
        migrate(db, recovery_root=disposable_vault.recovery_root)

        conn = _connect(db)
        try:
            conn.execute(
                "UPDATE schema_migrations SET outcome = ?, finished_at = NULL",
                (OUTCOME_RUNNING,),
            )
            conn.execute("UPDATE state_metadata SET schema_version = 0 WHERE id = 1")
        finally:
            conn.close()

        with pytest.raises(MigrationLedgerError) as exc:
            migrate(db, recovery_root=disposable_vault.recovery_root)
        assert exc.value.reason_code == "migration_incomplete"
        assert MIGRATIONS[0].migration_id in str(exc.value)


# ── Backup verification ───────────────────────────────────────────────────────


class TestVerifiedBackup:
    def test_backup_is_readable_hashed_and_matches_source(self, disposable_vault):
        db = _make_legacy_db(disposable_vault)
        backup = create_verified_backup(db, disposable_vault.recovery_root)

        assert backup.verified is True
        assert backup.path.exists()
        assert backup.size_bytes == backup.path.stat().st_size
        conn = _connect(backup.path)
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0] == 1
        finally:
            conn.close()

    def test_backup_captures_wal_resident_commits(self, disposable_vault):
        """The reason this uses SQLite's backup API and not shutil.copy: a
        committed row still sitting in the -wal sidecar must be in the
        backup. A raw copy of the main file alone would open cleanly and be
        silently missing it."""
        db = _make_legacy_db(disposable_vault)
        live = sqlite3.connect(str(db))
        try:
            live.execute("PRAGMA journal_mode=WAL")
            live.execute(
                "INSERT INTO archive (file_path, artist, title) VALUES (?, ?, ?)",
                ("/x/b.m4a", "The Byrds", "Eight Miles High"),
            )
            live.commit()
            backup = create_verified_backup(db, disposable_vault.recovery_root)
        finally:
            live.close()

        conn = _connect(backup.path)
        try:
            titles = {r[0] for r in conn.execute("SELECT title FROM archive")}
        finally:
            conn.close()
        assert "Eight Miles High" in titles

    def test_restore_refuses_a_backup_whose_bytes_moved(self, disposable_vault):
        db = _make_legacy_db(disposable_vault)
        backup = create_verified_backup(db, disposable_vault.recovery_root)
        with backup.path.open("ab") as handle:
            handle.write(b"tampered")

        with pytest.raises(BackupVerificationError) as exc:
            restore_from_backup(backup, db)
        assert "no longer matches its recorded digest" in str(exc.value)

    def test_restore_puts_the_rows_back(self, disposable_vault):
        """Negative control for the test above, and the mechanism MCR-004
        requires: a verified backup must actually restore."""
        db = _make_legacy_db(disposable_vault)
        backup = create_verified_backup(db, disposable_vault.recovery_root)

        conn = _connect(db)
        try:
            conn.execute("DELETE FROM archive")
            assert conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0] == 0
        finally:
            conn.close()

        restore_from_backup(backup, db)

        conn = _connect(db)
        try:
            row = conn.execute("SELECT artist, title FROM archive").fetchone()
        finally:
            conn.close()
        assert (row["artist"], row["title"]) == ("Bob Seger", "Night Moves")

    def test_missing_recovery_root_is_refused(self, disposable_vault, tmp_path):
        db = _make_legacy_db(disposable_vault)
        with pytest.raises(RecoveryTargetError) as exc:
            create_verified_backup(db, tmp_path / "nope")
        assert exc.value.reason_code == "recovery_target_invalid"


# ── Failure injection ─────────────────────────────────────────────────────────

_GOOD_FIRST = "CREATE TABLE p0_06_probe_a (id INTEGER PRIMARY KEY)"
_BROKEN_SECOND = "CREATE TABLE p0_06_probe_b (id INTEGER PRIMARY KEY, THIS IS NOT SQL)"


def _failing_chain() -> tuple[Migration, ...]:
    """The shipped chain plus one deliberately broken step on the end.

    Built from the whole of MIGRATIONS rather than from MIGRATIONS[0]:
    validate_registry() requires a contiguous chain, so hardcoding the
    first migration alone breaks the moment a real 0002 is added. It did,
    which is the registry check doing its job."""
    return (
        *MIGRATIONS,
        Migration(
            migration_id="9002_deliberately_broken",
            from_version=SCHEMA_VERSION,
            to_version=SCHEMA_VERSION + 1,
            statements=(_GOOD_FIRST, _BROKEN_SECOND),
            description="fixture-only: fails half way through, on purpose",
        ),
    )


class TestFailedMigrationLeavesPriorStateUsable:
    def test_transaction_rolls_back_the_partial_change(self, disposable_vault):
        db = _make_legacy_db(disposable_vault)
        chain = _failing_chain()

        with pytest.raises(MigrationFailedError) as exc:
            migrate(db, recovery_root=disposable_vault.recovery_root, migrations=chain)

        assert exc.value.reason_code == "migration_failed"
        assert exc.value.details["migration_id"] == "9002_deliberately_broken"

        conn = _connect(db)
        try:
            # The first migration committed; the second rolled back whole.
            assert read_schema_version(conn) == SCHEMA_VERSION
            assert table_exists(conn, "p0_06_probe_a") is False, (
                "the statement that succeeded before the failure must not survive"
            )
            assert table_exists(conn, "p0_06_probe_b") is False
            # And the legacy content is still there and readable.
            assert conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0] == 1
        finally:
            conn.close()

    def test_failure_is_recorded_in_the_ledger(self, disposable_vault):
        db = _make_legacy_db(disposable_vault)
        with pytest.raises(MigrationFailedError):
            migrate(db, recovery_root=disposable_vault.recovery_root, migrations=_failing_chain())

        rows = {r["migration_id"]: r for r in _ledger_rows(db)}
        for shipped in MIGRATIONS:
            assert rows[shipped.migration_id]["outcome"] == OUTCOME_SUCCEEDED
        failed = rows["9002_deliberately_broken"]
        assert failed["outcome"] == OUTCOME_FAILED
        assert failed["finished_at"] is not None
        assert failed["error_code"] is not None

    def test_the_named_backup_restores_the_pre_migration_database(self, disposable_vault):
        db = _make_legacy_db(disposable_vault)
        with pytest.raises(MigrationFailedError) as exc:
            migrate(db, recovery_root=disposable_vault.recovery_root, migrations=_failing_chain())

        backup_path = Path(exc.value.details["backup_path"])
        assert backup_path.exists()
        conn = _connect(backup_path)
        try:
            assert read_schema_version(conn) == LEGACY_UNVERSIONED
            assert conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0] == 1
        finally:
            conn.close()


# ── Candidate swap ────────────────────────────────────────────────────────────


def _candidate_chain(statements: tuple[str, ...]) -> tuple[Migration, ...]:
    return (
        *MIGRATIONS,
        Migration(
            migration_id="9002_candidate_swap",
            from_version=SCHEMA_VERSION,
            to_version=SCHEMA_VERSION + 1,
            statements=statements,
            description="fixture-only: exercises the non-transactional path",
            transactional=False,
        ),
    )


class TestCandidateSwap:
    def test_successful_swap_applies_and_preserves_content(self, disposable_vault):
        db = _make_legacy_db(disposable_vault)
        chain = _candidate_chain(("CREATE TABLE swapped_in (id INTEGER PRIMARY KEY)", "VACUUM"))

        result = migrate(db, recovery_root=disposable_vault.recovery_root, migrations=chain)

        assert result.applied == (*[m.migration_id for m in MIGRATIONS], "9002_candidate_swap")
        conn = _connect(db)
        try:
            assert read_schema_version(conn) == SCHEMA_VERSION + 1
            assert table_exists(conn, "swapped_in") is True
            assert conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0] == 1
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()
        assert not db.with_name(db.name + ".candidate").exists()

    def test_failed_candidate_leaves_the_active_database_untouched(self, disposable_vault):
        db = _make_legacy_db(disposable_vault)
        migrate(db, recovery_root=disposable_vault.recovery_root)
        before = snapshot_vault_state(disposable_vault)

        chain = _candidate_chain(("CREATE TABLE ok_first (id INTEGER)", "NOT VALID SQL AT ALL"))
        with pytest.raises(MigrationFailedError):
            migrate(db, recovery_root=disposable_vault.recovery_root, migrations=chain)

        conn = _connect(db)
        try:
            assert read_schema_version(conn) == SCHEMA_VERSION
            assert table_exists(conn, "ok_first") is False
            assert conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0] == 1
        finally:
            conn.close()
        assert not db.with_name(db.name + ".candidate").exists(), "candidate must be discarded"
        assert before.db_archive_count == snapshot_vault_state(disposable_vault).db_archive_count

    def test_swap_is_atomic_within_one_filesystem(self, disposable_vault):
        """The candidate is created beside the active database rather than
        in the recovery root, because os.replace is only atomic within one
        filesystem. Asserted structurally so a later refactor that 'tidies'
        the candidate into the recovery root fails here first."""
        db = _make_legacy_db(disposable_vault)
        seen: list[Path] = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append(Path(src))
            return real_replace(src, dst)

        chain = _candidate_chain(("CREATE TABLE swapped_in (id INTEGER)",))
        original = migrator_mod.os.replace
        migrator_mod.os.replace = spy
        try:
            migrate(db, recovery_root=disposable_vault.recovery_root, migrations=chain)
        finally:
            migrator_mod.os.replace = original

        assert seen, "candidate swap did not call os.replace"
        assert seen[0].parent == db.parent
        assert seen[0].parent != disposable_vault.recovery_root
