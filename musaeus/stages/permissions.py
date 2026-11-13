#!/usr/bin/env python3
"""
MUSAEUS — Stage: Permissions
Fix file/folder permissions across the vault (files copied from Windows/ExFAT
sources often land with wrong permissions).

Scope widened 2026-08-21 at Grey's request ("please have it fire on all
runs"). It previously swept ctx.inbox only, which is why its chmod path had
never once executed against real dirty data in a month of running: INBOX is
empty or already-correct most of the time, so the stage reported success
without ever doing anything. The permissions that actually drift are on
files the pipeline has already moved into the library -- a live check found
8 wrong-mode files in ALAC-Library and 0 in INBOX.

What it does:
  - Scans every file/dir under each root in _sweep_roots()
  - Flags anything not matching FILE_MODE (files) / DIR_MODE (dirs)
  - dry_run() reports what would be fixed, touches nothing
  - run() re-scans live (no reliance on a stale snapshot) and chmods
  - Logs a PERMISSION_FIXED event per item actually changed

Standalone from HealthStage/CorruptStage deliberately: those already cover
metadata-quality checks and corruption detection/quarantine respectively.
This is the one piece of the old (now-archived)
_ARCHIVE/dead_code_20260809/MUSAEUS_stale_working_copy health.py that
wasn't duplicated elsewhere — ported as its own small stage rather than
reviving that file.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import sys
from pathlib import Path

from ..context import RunContext, StageResult, elision
from .base import BaseStage

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 200

FILE_MODE = 0o644  # rw-r--r--
DIR_MODE = 0o755  # rwxr-xr-x


def _sweep_roots(ctx: RunContext) -> list[Path]:
    """Every tree this stage is responsible for, in pipeline order.

    Deliberately excludes _history/ (DB snapshots and the hash ledger are
    not audio and have their own access story) but is otherwise everything
    MUSAEUS itself writes -- intake, the working buffer, and both library
    tiers. Missing roots are skipped rather than treated as an error, since
    ALAC_Archive only exists once Phase 2A has run.
    """
    roots = [ctx.inbox, ctx.config.staging, ctx.alac_library]
    archive = ctx.config.alac_archive
    if archive.exists():
        roots.append(archive)
    return [r for r in roots if r is not None and r.exists()]


def _scan_permissions(root: Path) -> tuple[list[Path], list[Path]]:
    """Scan a directory tree for files/dirs with incorrect permissions."""
    bad_files: list[Path] = []
    bad_dirs: list[Path] = []

    if not root.exists():
        return bad_files, bad_dirs

    # Never through a link (Codex audit 2026-10-16, F4): a link dropped into
    # INBOX to a private file elsewhere had its TARGET opened up to 0644.
    # os.walk does not follow linked folders, and lstat sees the link itself;
    # a link is neither a regular file nor a directory, so it is skipped.
    paths: list[Path] = []
    links = 0
    for top, dirs, files in os.walk(root, followlinks=False):
        for n in dirs + files:
            p = Path(top) / n
            if p.is_symlink():
                links += 1
                continue
            paths.append(p)
    if links:
        logger.info("[permissions] %s: %d link(s) left alone", root, links)
    for path in paths:
        if "_history" in path.parts:
            continue
        try:
            st = path.lstat()
        except OSError as e:
            logger.warning("[permissions] cannot stat %s: %s", path, e)
            continue

        mode = stat.S_IMODE(st.st_mode)
        if sys.platform == "win32":
            # Windows has no POSIX permission bits: every file reads 0o666 and
            # would look wrong for ever (Windows trial 2026-11-12). The one
            # thing to mend there is the read-only flag, which stops tagging.
            if stat.S_ISREG(st.st_mode) and not mode & stat.S_IWRITE:
                bad_files.append(path)
        elif stat.S_ISDIR(st.st_mode):
            if mode != DIR_MODE:
                bad_dirs.append(path)
        elif stat.S_ISREG(st.st_mode) and mode != FILE_MODE:
            bad_files.append(path)

    return bad_files, bad_dirs


def _chmod_no_follow(path: Path, mode: int, want_dir: bool) -> None:
    """chmod *path* only if it is still the regular file or folder the scan saw,
    and never through a link: fchmodat with AT_SYMLINK_NOFOLLOW. Nothing is
    opened, so a FIFO swapped in cannot hang the stage (review of #203,
    finding 1). This covers the last part of the path only: a parent folder
    swapped for a link mid-run is still followed (finding 3)."""
    st = path.lstat()
    if not (stat.S_ISDIR if want_dir else stat.S_ISREG)(st.st_mode):
        raise OSError(f"no longer a {'folder' if want_dir else 'file'}: {path}")
    try:
        os.chmod(path, mode, follow_symlinks=False)
    except NotImplementedError as exc:
        # A link swapped in after the lstat -- or Windows before Python 3.13,
        # where chmod cannot be told not to follow one. The lstat above has
        # just seen a real file, so there the plain call is the one left.
        if sys.platform != "win32":
            raise OSError(
                f"could not chmod without following links (a link, or no support here): {path}"
            ) from exc
        os.chmod(path, mode)


def _disk_ignores_modes(path: Path, mode: int, error: OSError | None) -> bool:
    """True when the disk under *path* has no Unix permissions to set: exFAT,
    NTFS and vfat take their modes from the mount. chmod there either does
    nothing, or refuses the file's own owner -- which a Unix disk never does."""
    if sys.platform == "win32":
        return False
    try:
        st = path.lstat()
    except OSError:
        return False
    if error is None:
        return stat.S_IMODE(st.st_mode) != mode
    return error.errno in (errno.EPERM, errno.ENOTSUP) and st.st_uid == os.geteuid()


def _scan_all(
    ctx: RunContext, skip: frozenset[Path] = frozenset()
) -> tuple[list[Path], list[Path]]:
    """Scan every swept root not in *skip*, merging the results."""
    all_files: list[Path] = []
    all_dirs: list[Path] = []
    for root in _sweep_roots(ctx):
        if root in skip:
            continue
        f, d = _scan_permissions(root)
        logger.info("[permissions] %s: %d file(s), %d dir(s) to fix", root.name, len(f), len(d))
        all_files.extend(f)
        all_dirs.extend(d)
    return all_files, all_dirs


class PermissionsStage(BaseStage):
    """
    Permissions sweep — fix file/folder permissions under the inbox.
    """

    NAME = "permissions"

    def __init__(self) -> None:
        super().__init__()
        #: Roots on a disk with no Unix permissions, found by this run.
        self._no_modes: set[Path] = set()

    # ── Validate ──────────────────────────────────────────────────────────────

    def validate(self, ctx: RunContext) -> None:
        if not ctx.inbox.exists():
            logger.info("[permissions] inbox does not exist yet: %s", ctx.inbox)

    # ── Dry run ───────────────────────────────────────────────────────────────

    def dry_run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=True)

        bad_files, bad_dirs = _scan_all(ctx)
        result.files_processed = len(bad_files) + len(bad_dirs)
        result.files_changed = result.files_processed

        if not bad_files and not bad_dirs:
            result.notes.append("✓ All file/folder permissions correct.")
        else:
            result.notes.append(f"Would fix {len(bad_files)} file(s) and {len(bad_dirs)} dir(s):")
            for p in (bad_files + bad_dirs)[:20]:
                kind = "dir " if p in bad_dirs else "file"
                try:
                    mode = oct(stat.S_IMODE(p.lstat().st_mode))
                except OSError:
                    mode = "?"
                want = oct(DIR_MODE) if p in bad_dirs else oct(FILE_MODE)
                result.notes.append(f"  [{kind}] {p} ({mode} -> {want})")
            remaining = len(bad_files) + len(bad_dirs) - 20
            if remaining > 0:
                result.notes.append(f"  {elision(remaining)}")

        ctx.record_stage(result)
        return result

    # ── Run ───────────────────────────────────────────────────────────────────

    def verify_effect(self, ctx: RunContext, result: StageResult) -> list[str]:
        """After a repair pass, a re-scan must find fewer problems.

        The fault this guards: the stage swept INBOX only, which is empty
        or already-correct almost always, so it reported success for a
        month without its chmod path ever executing.
        """
        bad_files, bad_dirs = _scan_all(ctx, frozenset(self._no_modes))
        remaining = len(bad_files) + len(bad_dirs)
        if remaining == 0:
            return []
        return [
            f"fixed {result.files_changed} item(s) but a re-scan still finds "
            f"{remaining} with wrong permissions"
        ]

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)
        self._no_modes.clear()
        fixed = {False: 0, True: 0}  # by "is a folder"

        # Live re-scan -- never trust a snapshot taken earlier.
        for root in _sweep_roots(ctx):
            bad_files, bad_dirs = _scan_permissions(root)
            todo = [(f, FILE_MODE, False) for f in bad_files] + [
                (d, DIR_MODE, True) for d in bad_dirs
            ]
            result.files_processed += len(todo)
            for path, mode, is_dir in todo:
                error: OSError | None = None
                try:
                    _chmod_no_follow(path, mode, want_dir=is_dir)
                except OSError as e:
                    error = e
                if _disk_ignores_modes(path, mode, error):
                    # exFAT, NTFS, vfat: every file looked wrong and every fix
                    # failed, run after run. One try is enough to know.
                    self._no_modes.add(root)
                    result.notes.append(
                        f"{root}: this disk has no Unix permissions; "
                        f"{len(todo)} item(s) left alone."
                    )
                    break
                if error is not None:
                    logger.warning("[permissions] could not fix %s: %s", path, error)
                    result.files_errored += 1
                    result.errors.append(f"{path}: {error}")
                    continue
                fixed[is_dir] += 1
                result.files_changed += 1
                ctx.log_event(
                    "PERMISSION_FIXED",
                    file_path=str(path),
                    new_value=oct(mode),
                    stage=self.NAME,
                    note=f"{'directory' if is_dir else 'file'} permission corrected",
                )
                # After a fix only: a failure used to commit every time.
                if result.files_changed % _COMMIT_EVERY == 0:
                    ctx.conn.commit()

        if result.files_errored or result.files_changed:
            failed = f" Could not fix {result.files_errored}." if result.files_errored else ""
            result.notes.append(f"Fixed {fixed[False]} file(s) and {fixed[True]} dir(s).{failed}")
        elif not self._no_modes:
            result.notes.append("✓ All file/folder permissions correct.")

        ctx.record_stage(result)
        return result
