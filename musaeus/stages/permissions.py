#!/usr/bin/env python3
"""
MUSAEUS — Stage: Permissions
Fix file/folder permissions under the inbox (files copied from Windows/ExFAT
sources often land with wrong permissions).

What it does:
  - Scans every file/dir under ctx.inbox
  - Flags anything not matching FILE_MODE (files) / DIR_MODE (dirs)
  - dry_run() reports what would be fixed, touches nothing
  - run() re-scans live (no reliance on a stale snapshot) and chmods
  - Logs a PERMISSION_FIXED event per item actually changed

Standalone from HealthStage/CorruptStage deliberately: those already cover
metadata-quality checks and corruption detection/quarantine respectively.
This is the one piece of the old (now-archived)
_ARCHIVES/dead_code_20260809/MUSAEUS_stale_working_copy health.py that
wasn't duplicated elsewhere — ported as its own small stage rather than
reviving that file.
"""

from __future__ import annotations

import logging
import stat
from pathlib import Path

from ..context import RunContext, StageResult
from .base import BaseStage

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 200

FILE_MODE = 0o644  # rw-r--r--
DIR_MODE = 0o755   # rwxr-xr-x


def _scan_permissions(root: Path) -> tuple[list[Path], list[Path]]:
    """Scan a directory tree for files/dirs with incorrect permissions."""
    bad_files: list[Path] = []
    bad_dirs: list[Path] = []

    if not root.exists():
        return bad_files, bad_dirs

    for path in root.rglob("*"):
        try:
            st = path.stat()
        except OSError as e:
            logger.warning("[permissions] cannot stat %s: %s", path, e)
            continue

        mode = stat.S_IMODE(st.st_mode)
        if stat.S_ISDIR(st.st_mode):
            if mode != DIR_MODE:
                bad_dirs.append(path)
        elif stat.S_ISREG(st.st_mode):
            if mode != FILE_MODE:
                bad_files.append(path)

    return bad_files, bad_dirs


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

        bad_files, bad_dirs = _scan_permissions(ctx.inbox)
        result.files_processed = len(bad_files) + len(bad_dirs)
        result.files_changed = result.files_processed

        if not bad_files and not bad_dirs:
            result.notes.append("✓ All file/folder permissions correct.")
        else:
            result.notes.append(
                f"Would fix {len(bad_files)} file(s) and {len(bad_dirs)} dir(s):"
            )
            for p in (bad_files + bad_dirs)[:20]:
                kind = "dir " if p in bad_dirs else "file"
                try:
                    mode = oct(stat.S_IMODE(p.stat().st_mode))
                except OSError:
                    mode = "?"
                want = oct(DIR_MODE) if p in bad_dirs else oct(FILE_MODE)
                result.notes.append(f"  [{kind}] {p} ({mode} -> {want})")
            remaining = len(bad_files) + len(bad_dirs) - 20
            if remaining > 0:
                result.notes.append(f"  ... and {remaining} more")

        ctx.record_stage(result)
        return result

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self, ctx: RunContext) -> StageResult:
        result = self._make_result(dry_run=False)

        # Live re-scan -- never trust a snapshot taken earlier.
        bad_files, bad_dirs = _scan_permissions(ctx.inbox)
        result.files_processed = len(bad_files) + len(bad_dirs)

        fixed_files = 0
        fixed_dirs = 0

        for f in bad_files:
            try:
                f.chmod(FILE_MODE)
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
                d.chmod(DIR_MODE)
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
            result.notes.append(
                f"Fixed {fixed_files} file(s) and {fixed_dirs} dir(s)."
            )

        ctx.record_stage(result)
        return result
