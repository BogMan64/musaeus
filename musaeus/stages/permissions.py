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

import logging
import os
import stat
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
        if stat.S_ISDIR(st.st_mode):
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
    except NotImplementedError as exc:  # a link swapped in after the lstat
        raise OSError(f"could not chmod without following links (a link, or no support here): {path}") from exc


def _scan_all(ctx: RunContext) -> tuple[list[Path], list[Path]]:
    """Scan every swept root, merging the results."""
    all_files: list[Path] = []
    all_dirs: list[Path] = []
    for root in _sweep_roots(ctx):
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
        bad_files, bad_dirs = _scan_all(ctx)
        remaining = len(bad_files) + len(bad_dirs)
        if remaining == 0:
            return []
        return [
            f"fixed {result.files_changed} item(s) but a re-scan still finds "
            f"{remaining} with wrong permissions"
        ]

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)

        # Live re-scan -- never trust a snapshot taken earlier.
        bad_files, bad_dirs = _scan_all(ctx)
        result.files_processed = len(bad_files) + len(bad_dirs)

        fixed_files = 0
        fixed_dirs = 0

        for f in bad_files:
            try:
                _chmod_no_follow(f, FILE_MODE, want_dir=False)
                fixed_files += 1
                result.files_changed += 1
                ctx.log_event(
                    "PERMISSION_FIXED",
                    file_path=str(f),
                    new_value=oct(FILE_MODE),
                    stage=self.NAME,
                    note="file permission corrected",
                )
            except OSError as e:
                logger.warning("[permissions] could not fix %s: %s", f, e)
                result.files_errored += 1
                result.errors.append(f"{f}: {e}")

            if result.files_processed and (fixed_files + fixed_dirs) % _COMMIT_EVERY == 0:
                ctx.conn.commit()

        for d in bad_dirs:
            try:
                _chmod_no_follow(d, DIR_MODE, want_dir=True)
                fixed_dirs += 1
                result.files_changed += 1
                ctx.log_event(
                    "PERMISSION_FIXED",
                    file_path=str(d),
                    new_value=oct(DIR_MODE),
                    stage=self.NAME,
                    note="directory permission corrected",
                )
            except OSError as e:
                logger.warning("[permissions] could not fix %s: %s", d, e)
                result.files_errored += 1
                result.errors.append(f"{d}: {e}")

            if (fixed_files + fixed_dirs) % _COMMIT_EVERY == 0:
                ctx.conn.commit()

        if fixed_files == 0 and fixed_dirs == 0:
            result.notes.append("✓ All file/folder permissions correct.")
        else:
            result.notes.append(f"Fixed {fixed_files} file(s) and {fixed_dirs} dir(s).")

        ctx.record_stage(result)
        return result
