"""Verified, checksummed database backups.

Every migration in :mod:`musaeus.state.migrate` takes a backup through this
module before it changes the active database. A backup is only considered
usable once its checksum has been independently re-read from disk and its
integrity has been verified with SQLite's own ``PRAGMA integrity_check`` —
"the copy exists" is not "the copy is verified."

Policy boundary: this module never chooses a backup location. Every function
requires an explicit ``backup_dir`` argument supplied by the caller (in P0-06,
always a disposable fixture directory). There is no fallback to the fixed
future live recovery root, and this module does not create or probe it.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


def _utc_now_compact() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _read_only_sqlite_uri(path: Path) -> str:
    """Build a ``file:...?mode=ro`` URI with the path percent-encoded.

    A raw path inserted into a URI is unsafe if it contains ``?``, ``#``, or
    ``%`` — SQLite would parse the remainder as query text and open the
    wrong file (or fail outright). ``quote`` with a permissive safe-set
    keeps normal POSIX paths readable while still encoding those characters.
    """
    return f"file:{quote(path.as_posix(), safe='/:')}?mode=ro"


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BackupVerificationError(RuntimeError):
    """Raised when a backup cannot be verified as a complete, readable copy."""


@dataclass(frozen=True)
class VerifiedBackup:
    """A checksummed, integrity-checked copy of a database file.

    ``source_path`` and ``backup_path`` are recorded so a caller (or a report)
    can trace which live file this backup corresponds to without re-deriving
    it. ``checksum`` is the SHA-256 of the backup file's bytes as re-read from
    disk after the copy completed — not the source's checksum — so a silent
    truncation or corruption during copy is detected rather than assumed away.
    """

    source_path: Path
    backup_path: Path
    checksum: str
    created_at: str

    def verify(self) -> None:
        """Re-check readability, checksum, and SQLite integrity on demand.

        Raises :class:`BackupVerificationError` on any mismatch or failure.
        Safe to call repeatedly; performs no writes.
        """
        if not self.backup_path.is_file():
            raise BackupVerificationError(f"Backup file missing: {self.backup_path}")
        actual_checksum = _sha256_of(self.backup_path)
        if actual_checksum != self.checksum:
            raise BackupVerificationError(
                f"Backup checksum mismatch for {self.backup_path}: "
                f"recorded={self.checksum} actual={actual_checksum}"
            )
        _assert_sqlite_integrity(self.backup_path)


def _assert_sqlite_integrity(db_path: Path) -> None:
    """Open *db_path* read-only and run ``PRAGMA integrity_check``.

    Raises :class:`BackupVerificationError` if the file cannot be opened as a
    SQLite database or if the integrity check reports anything other than
    the single expected ``ok`` row.
    """
    uri = _read_only_sqlite_uri(db_path)
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise BackupVerificationError(f"Backup is not a readable SQLite file: {exc}") from exc
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as exc:
        raise BackupVerificationError(f"Backup failed integrity_check: {exc}") from exc
    finally:
        conn.close()
    results = [row[0] for row in rows]
    if results != ["ok"]:
        raise BackupVerificationError(
            f"Backup integrity_check reported problems for {db_path}: {results}"
        )


def create_verified_backup(db_path: Path, backup_dir: Path) -> VerifiedBackup:
    """Copy *db_path* into *backup_dir* and verify the copy before returning.

    Uses SQLite's own online backup API (``sqlite3.Connection.backup``) rather
    than a raw filesystem copy, so a backup taken while WAL-mode writers exist
    is still a consistent snapshot rather than a torn read of the main file.

    Raises :class:`BackupVerificationError` if the resulting copy fails
    checksum re-read or ``PRAGMA integrity_check``. Never touches any path
    other than *db_path* (read-only) and *backup_dir* (the new backup file).
    """
    if not db_path.is_file():
        raise BackupVerificationError(f"Cannot back up a database that does not exist: {db_path}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{db_path.stem}.{_utc_now_compact()}.backup{db_path.suffix}"

    source_conn = sqlite3.connect(_read_only_sqlite_uri(db_path), uri=True)
    try:
        dest_conn = sqlite3.connect(str(backup_path))
        try:
            source_conn.backup(dest_conn)
            dest_conn.commit()
        finally:
            dest_conn.close()
    finally:
        source_conn.close()

    checksum = _sha256_of(backup_path)
    backup = VerifiedBackup(
        source_path=db_path,
        backup_path=backup_path,
        checksum=checksum,
        created_at=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    )
    # Verify immediately: a backup that cannot pass its own verification is
    # never handed back to a migration caller as if it were usable recovery.
    backup.verify()
    return backup


def restore_from_backup(backup: VerifiedBackup, target_path: Path) -> None:
    """Restore *backup* to *target_path*, verifying the backup first.

    This is a plain file copy (SQLite files are self-contained), performed
    only after :meth:`VerifiedBackup.verify` passes. It does not delete
    *target_path* first — an existing file at *target_path* is overwritten by
    ``shutil.copy2`` semantics, matching a caller-controlled restore/rollback
    step rather than an implicit destructive replace of an unrelated file.
    """
    backup.verify()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup.backup_path, target_path)
