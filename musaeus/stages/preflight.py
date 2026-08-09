#!/usr/bin/env python3
"""
MUSAEUS — Preflight Stage

Environment/dependency sanity checks, run before the real pipeline stages.
Ported from ORPHEUS's SCRIPTS/preflight_orpheus.py structure: an OK/WARN/FAIL
accumulator that runs every check regardless of earlier failures, then reports
everything at once. The stage itself never aborts — validate() raises
StageError only if something makes every other check meaningless (e.g. the
config/paths can't even be resolved). Otherwise it's the CALLER's decision
whether a FAIL should block the rest of the run, matching ORPHEUS's own
split: interactive/console-style callers can treat a FAIL as fatal, while an
unattended overnight run can log it and continue.

What it checks, in order:
  1. Required directories exist / are creatable (inbox, staging, quarantine,
     runs, meta_dir) and are writable.
  2. External commands MUSAEUS actually depends on are on PATH: ffmpeg,
     ffprobe (loudness/transcode/integrity/auditor/albumart), fpcalc
     (acousticid). Each failure includes a concrete install hint.
  3. Python packages MUSAEUS actually imports: mutagen (hard dependency),
     rapidfuzz (soft dependency -- neardupe/canon degrade without it, not a
     hard failure, matching how those modules already guard the import).
  4. Disk space on the vault's filesystem -- two-tier fail/warn, wired to
     the same MIN_FREE_GB convention musaeus_overnight.sh already uses
     (not a new hardcoded threshold).
  5. Database integrity: PRAGMA integrity_check, plus a basic connectivity
     + row-count sanity check. ORPHEUS treats this as a hard-stop-worthy
     condition (interactive callers get a "continue anyway?" prompt); here
     a FAIL is reported the same way as everything else and left to the
     caller.
  6. Stale WAL/SHM files from a previous crashed run (cleanup, not a
     failure -- matches ORPHEUS's cleanup_stale_wal_shm()).

Unlike ORPHEUS's version, this only checks what MUSAEUS's own stages
actually import/call (no PIL/requests/itunespy -- MUSAEUS doesn't use
them). See module docstring history if that changes.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from pathlib import Path

from ..context import RunContext, StageResult
from .base import BaseStage, StageError

logger = logging.getLogger(__name__)

# Two-tier disk space thresholds (GB). Matches musaeus_overnight.sh's
# MIN_FREE_GB=50 convention for the "unusable" floor; WARN_FREE_GB is a
# softer heads-up tier below which things are getting tight but not yet
# blocked.
FAIL_FREE_GB = 50
WARN_FREE_GB = 100

# External commands each real MUSAEUS stage depends on, with the stage(s)
# that need them and an install hint if missing.
_REQUIRED_COMMANDS: tuple[tuple[str, str, str], ...] = (
    ("ffmpeg", "forge, transcode, integrity, auditor, albumart", "sudo apt install ffmpeg"),
    ("ffprobe", "forge, transcode, integrity, auditor, albumart", "sudo apt install ffmpeg"),
    ("fpcalc", "acousticid", "sudo apt install libchromaprint-tools"),
)


class PreflightStage(BaseStage):
    """
    Preflight — environment sanity checks. Report-only: never mutates
    files or the archive table. Always safe to run, including in dry-run.
    """

    NAME = "preflight"

    def validate(self, ctx: RunContext) -> None:
        # Nothing to validate before validating -- if ctx/config didn't
        # resolve, RunContext construction itself would already have failed.
        pass

    # ── Individual checks ────────────────────────────────────────────────────

    def _check_directories(self, ctx: RunContext, ok: list, warn: list, fail: list) -> None:
        dirs = {
            "inbox": ctx.inbox,
            "staging": ctx.staging,
            "quarantine": ctx.quarantine,
            "runs_root": ctx.runs_root,
        }
        for label, path in dirs.items():
            if not path.exists():
                try:
                    path.mkdir(parents=True, exist_ok=True)
                    ok.append(f"{label}: created {path}")
                except OSError as exc:
                    fail.append(f"{label}: cannot create {path} ({exc})")
                    continue
            else:
                ok.append(f"{label}: exists ({path})")

            # Writability check: write + delete a marker file.
            probe = path / ".musaeus_preflight_write_test"
            try:
                probe.write_text("ok")
                probe.unlink()
            except OSError as exc:
                fail.append(f"{label}: not writable ({path}): {exc}")

    def _check_commands(self, ok: list, warn: list, fail: list) -> None:
        for cmd, used_by, hint in _REQUIRED_COMMANDS:
            found = shutil.which(cmd)
            if found:
                ok.append(f"{cmd}: found ({found})")
            else:
                fail.append(f"{cmd}: not found on PATH -- required by {used_by}. Fix: {hint}")

    def _check_packages(self, ok: list, warn: list, fail: list) -> None:
        # Hard dependency: pyproject.toml declares this, tagger/forge import it.
        try:
            import mutagen  # noqa: F401
            ok.append(f"mutagen: available (v{mutagen.version_string})")
        except ImportError:
            fail.append("mutagen: not installed -- required by forge, tagger. Fix: pip install mutagen")

        # Soft dependency: neardupe/canon already guard this import and
        # degrade gracefully (NearDupeStage.validate() raises StageError
        # itself if missing when actually invoked) -- warn here, don't fail,
        # so a preflight run doesn't block stages that don't need it.
        try:
            import rapidfuzz  # noqa: F401
            ok.append("rapidfuzz: available")
        except ImportError:
            warn.append(
                "rapidfuzz: not installed -- neardupe and fuzzy artist/genre "
                "canon matching will be unavailable. Fix: pip install rapidfuzz"
            )

    def _check_disk_space(self, ctx: RunContext, ok: list, warn: list, fail: list) -> None:
        try:
            usage = shutil.disk_usage(str(ctx.vault_root))
            free_gb = usage.free / (1024**3)
            label = f"disk free on {ctx.vault_root}: {free_gb:.1f} GB"
            if free_gb < FAIL_FREE_GB:
                fail.append(f"{label} (< {FAIL_FREE_GB} GB -- critically low)")
            elif free_gb < WARN_FREE_GB:
                warn.append(f"{label} (< {WARN_FREE_GB} GB -- getting tight)")
            else:
                ok.append(label)
        except OSError as exc:
            warn.append(f"could not check disk space on {ctx.vault_root}: {exc}")

    def _check_db_integrity(self, ctx: RunContext, ok: list, warn: list, fail: list) -> None:
        try:
            rows = ctx.conn.execute("PRAGMA integrity_check").fetchall()
            integrity = "; ".join(r[0] for r in rows) if rows else "ok"
            if integrity == "ok":
                ok.append(f"db integrity_check: ok ({ctx.config.db_path})")
            else:
                fail.append(f"db integrity_check FAILED: {integrity} ({ctx.config.db_path})")
        except sqlite3.Error as exc:
            fail.append(f"db integrity_check could not run: {exc}")
            return

        try:
            count = ctx.conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
            ok.append(f"archive table: accessible, {count} row(s)")
        except sqlite3.Error as exc:
            fail.append(f"archive table not accessible: {exc}")

    def _check_stale_wal(self, ctx: RunContext, ok: list, warn: list, fail: list) -> None:
        """
        Report leftover -wal/-shm files from a previous crashed process.
        Report-only here (not deleted) -- unlike ORPHEUS's
        cleanup_stale_wal_shm(), preflight must not mutate anything before
        the caller has decided whether to proceed. A live -wal/-shm pair is
        completely normal for an open WAL-mode connection (this stage's own
        ctx.conn included), so this is informational, never a fail.
        """
        db_path = ctx.config.db_path
        for suffix in ("-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                ok.append(f"{p.name}: present ({p.stat().st_size} bytes) -- normal for an open WAL connection")

    # ── Shared logic ──────────────────────────────────────────────────────────

    def _run_checks(self, ctx: RunContext) -> tuple[list, list, list]:
        ok: list[str] = []
        warn: list[str] = []
        fail: list[str] = []

        self._check_directories(ctx, ok, warn, fail)
        self._check_commands(ok, warn, fail)
        self._check_packages(ok, warn, fail)
        self._check_disk_space(ctx, ok, warn, fail)
        self._check_db_integrity(ctx, ok, warn, fail)
        self._check_stale_wal(ctx, ok, warn, fail)

        return ok, warn, fail

    def _report(self, ctx: RunContext, dry_run: bool) -> StageResult:
        result = self._make_result(dry_run=dry_run)
        ok, warn, fail = self._run_checks(ctx)

        result.files_processed = len(ok) + len(warn) + len(fail)
        result.files_changed = len(ok)
        result.files_skipped = len(warn)
        result.files_errored = len(fail)

        for item in ok:
            result.notes.append(f"  OK    {item}")
        for item in warn:
            result.notes.append(f"  WARN  {item}")
        for item in fail:
            result.notes.append(f"  FAIL  {item}")
            result.errors.append(item)
            logger.error("[preflight] %s", item)

        result.notes.append(
            f"Preflight: {len(ok)} OK, {len(warn)} WARN, {len(fail)} FAIL"
        )

        # Report-only: preflight itself never fails the stage result over a
        # FAIL condition -- it's a report, and the caller (interactive CLI,
        # overnight script, or a future pipeline runner) decides whether a
        # FAIL should block the rest of the run. This mirrors ORPHEUS's own
        # split (console pipelines abort on preflight rc!=0, the overnight
        # pulse logs and continues) rather than baking one policy in here.
        ctx.record_stage(result)
        return result

    def dry_run(self, ctx: RunContext) -> StageResult:
        return self._report(ctx, dry_run=True)

    def run(self, ctx: RunContext) -> StageResult:
        return self._report(ctx, dry_run=False)
