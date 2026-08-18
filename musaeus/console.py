#!/usr/bin/env python3
"""
MUSAEUS — Interactive Pipeline Console

A clean, stage-aware terminal UI for running and inspecting the Musaeus pipeline.

Design:
  - Pure stdlib: curses-free. Works over SSH, in tmux, with no extras.
  - All output goes through a single _print() so it's easy to redirect.
  - Menu-driven: numbered choices, never bare input() for pipeline commands.
  - every interactive prompt guards against EOF (non-TTY / piped input).
  - No global state — Console is instantiated, used, and discarded.
  - dry_run mode is first-class: "Preview" always available before "Run".
  - Graceful degradation: if DB is unreachable, shows error and offers retry.

Launch:
    python -m musaeus.console
  or via CLI:
    musaeus console
"""

from __future__ import annotations

import contextlib
import logging
import sys
import traceback
from pathlib import Path

from .config import MusicConfig, get_config
from .context import RunContext
from .db import open_db
from .hasher import ffmpeg_available, ffprobe_available
from .stages import (
    DEFAULT_PIPELINE,
    CuratorStage,
    ForgeStage,
    IngestStage,
    PlaylistStage,
    ScholarStage,
    SentinelStage,
    TaggerStage,
)
from .stages.base import BaseStage

logger = logging.getLogger(__name__)

# ── Terminal helpers ──────────────────────────────────────────────────────────

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_BLUE = "\033[34m"
_MAGENTA = "\033[35m"

def _c(text: str, *codes: str) -> str:
    """Wrap text in ANSI codes if stdout is a TTY."""
    if not sys.stdout.isatty():
        return text
    return "".join(codes) + text + _RESET


def _hr(char: str = "─", width: int = 60) -> str:
    return _c(char * width, _DIM)


def _header(title: str) -> None:
    w = 60
    print()
    print(_c("╔" + "═" * (w - 2) + "╗", _CYAN, _BOLD))
    padded = f"  {title}  "
    padding = w - 2 - len(padded)
    print(_c("║" + padded + " " * padding + "║", _CYAN, _BOLD))
    print(_c("╚" + "═" * (w - 2) + "╝", _CYAN, _BOLD))


def _section(title: str) -> None:
    print()
    print(_c(f"  ▸ {title}", _BOLD, _CYAN))
    print(_hr())


def _ok(msg: str) -> None:
    print(_c(f"  ✓ {msg}", _GREEN))


def _warn(msg: str) -> None:
    print(_c(f"  ⚠ {msg}", _YELLOW))


def _err(msg: str) -> None:
    print(_c(f"  ✗ {msg}", _RED))


def _info(msg: str) -> None:
    print(f"    {msg}")


def _prompt(prompt: str, default: str = "") -> str:
    """
    Read one line of input. Handles EOF (non-TTY) by returning default.
    Never blocks a pipeline.
    """
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"\n  {prompt}{suffix}: ").strip()
        return val if val else default
    except EOFError:
        return default


def _choose(prompt: str, options: list[str], default: str = "0") -> str:
    """
    Present a numbered menu and return the chosen value.
    Returns default on EOF.
    """
    print()
    for i, opt in enumerate(options):
        print(f"    {_c(str(i), _BOLD, _CYAN)}  {opt}")
    try:
        val = input(f"\n  {prompt} [0-{len(options)-1}]: ").strip()
        return val if val else default
    except EOFError:
        return default


# ── Console class ─────────────────────────────────────────────────────────────

class Console:
    """
    Interactive Musaeus pipeline console.

    Usage:
        con = Console()
        con.run()
    """

    VERSION = "0.1.0"

    def __init__(self) -> None:
        self._config: MusicConfig | None = None
        self._running = True

    # ── Boot ──────────────────────────────────────────────────────────────────

    def _load_config(self) -> MusicConfig | None:
        try:
            cfg = get_config()
            return cfg
        except ValueError as exc:
            _err(str(exc))
            return None

    def _boot_check(self) -> bool:
        """Validate environment on startup. Returns True if OK to continue."""
        _section("System Check")
        ok = True

        cfg = self._load_config()
        if cfg is None:
            _err("Cannot start — MUSAEUS_VAULT_ROOT is not set.")
            _info("Set it in ~/.config/musaeus/settings.env and restart.")
            return False
        self._config = cfg

        _ok(f"Vault     : {cfg.vault_root}")
        _ok(f"Inbox     : {cfg.inbox}")
        _ok(f"DB        : {cfg.db_path}")

        if ffmpeg_available():
            _ok("ffmpeg    : found")
        else:
            _warn("ffmpeg    : NOT FOUND — audio hashing disabled (falling back to full-file hash)")
            ok = True  # degraded but operational

        if ffprobe_available():
            _ok("ffprobe   : found")
        else:
            _warn("ffprobe   : NOT FOUND — Scholar stage will fail")

        if cfg.groq_api_key:
            _ok("Groq API  : configured")
        else:
            _warn("Groq API  : not set (AI features unavailable)")

        # Ensure directories exist
        try:
            cfg.ensure_dirs()
        except OSError as exc:
            _err(f"Cannot create directories: {exc}")
            return False

        return ok

    # ── DB helpers ────────────────────────────────────────────────────────────

    def _open_db(self):  # type: ignore[return]
        assert self._config is not None
        try:
            return open_db(self._config.db_path)
        except Exception as exc:
            _err(f"Cannot open DB: {exc}")
            return None

    # ── Status display ────────────────────────────────────────────────────────

    def _show_status(self) -> None:
        _section("Library Status")
        conn = self._open_db()
        if conn is None:
            return
        try:
            total = conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM archive WHERE status='PENDING'"
            ).fetchone()[0]
            hashed = conn.execute(
                "SELECT COUNT(*) FROM archive WHERE status='HASHED'"
            ).fetchone()[0]
            catalogued = conn.execute(
                "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'"
            ).fetchone()[0]
            dupes = conn.execute(
                "SELECT COUNT(DISTINCT group_id) FROM duplicates WHERE status='pending'"
            ).fetchone()[0]
            issues = conn.execute(
                "SELECT COUNT(*) FROM validation_issues"
            ).fetchone()[0]
            last_run = conn.execute(
                "SELECT MAX(ts) FROM events WHERE event_type='RUN_START'"
            ).fetchone()[0]

            forged = conn.execute(
                "SELECT COUNT(*) FROM archive WHERE rg_tagged_at IS NOT NULL"
            ).fetchone()[0]
            car_exported = conn.execute(
                "SELECT COUNT(*) FROM archive WHERE car_export_path IS NOT NULL"
            ).fetchone()[0]

            _info(f"Total files   : {_c(str(total), _BOLD)}")
            _info(f"  PENDING     : {pending}")
            _info(f"  HASHED      : {hashed}")
            _info(f"  CATALOGUED  : {catalogued}")
            _info(f"  FORGED      : {forged}  (RG tagged)")
            _info(f"  CAR EXPORT  : {car_exported}")
            _info(f"Dupe groups   : {_c(str(dupes), _YELLOW) if dupes else '0'}")
            _info(f"Issues        : {_c(str(issues), _YELLOW) if issues else '0'}")
            _info(f"Last run      : {last_run or 'never'}")

            # Inbox snapshot
            assert self._config is not None
            inbox = self._config.inbox
            if inbox.exists():
                from .config import AUDIO_EXTENSIONS
                inbox_count = sum(
                    1 for f in inbox.rglob("*")
                    if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
                )
                _info(f"Inbox files   : {_c(str(inbox_count), _CYAN)}")
            else:
                _warn(f"Inbox missing : {inbox}")

        finally:
            conn.close()

    # ── Run pipeline ──────────────────────────────────────────────────────────

    def _run_pipeline(self, dry_run: bool) -> None:
        mode = "DRY RUN" if dry_run else "LIVE RUN"
        _section(f"Pipeline  [{mode}]")

        assert self._config is not None
        conn = self._open_db()
        if conn is None:
            return

        try:
            try:
                ctx = RunContext.new(self._config, conn, dry_run=dry_run)
                stages: list[BaseStage] = [cls() for cls in DEFAULT_PIPELINE]

                for stage in stages:
                    print()
                    print(_c(f"  ── {stage.NAME.upper()} ──", _BOLD, _BLUE))
                    result = stage.execute(ctx)

                    if result.success:
                        _ok(result.summarise())
                    else:
                        _err(result.summarise())

                    for note in result.notes:
                        _info(note)
                    for err in result.errors:
                        _err(f"  ERROR: {err}")

                print()
                all_ok = all(r.success for r in ctx.stage_results)
                if all_ok:
                    _ok(f"Pipeline complete  run_id={ctx.run_id}")
                else:
                    _warn(f"Pipeline finished with errors  run_id={ctx.run_id}")

                ctx.finish()

            except Exception:
                _err("Pipeline crashed — see traceback below")
                traceback.print_exc()
        finally:
            # Guaranteed close on every exit path -- including
            # KeyboardInterrupt, which is a BaseException and would
            # otherwise skip both ctx.finish()'s close and the
            # except-Exception branch above, leaking an open connection
            # that then locks out any later stage run in this same
            # process (the exact bug behind two "database is locked"
            # crashes in one session, 2026-08-11). Safe to call even
            # after ctx.finish() already closed it -- closing an
            # already-closed sqlite3.Connection is a documented no-op.
            with contextlib.suppress(Exception):
                conn.close()

    # ── Run single stage ──────────────────────────────────────────────────────

    def _run_stage(self, stage_cls: type[BaseStage], dry_run: bool) -> None:
        mode = "DRY RUN" if dry_run else "LIVE RUN"
        assert self._config is not None
        conn = self._open_db()
        if conn is None:
            return
        try:
            try:
                ctx = RunContext.new(self._config, conn, dry_run=dry_run)
                stage = stage_cls()
                _section(f"{stage.NAME.upper()}  [{mode}]")
                result = stage.execute(ctx)
                if result.success:
                    _ok(result.summarise())
                else:
                    _err(result.summarise())
                for note in result.notes:
                    _info(note)
                for err_msg in result.errors:
                    _err(f"  ERROR: {err_msg}")
                ctx.finish()
            except Exception:
                _err("Stage crashed — see traceback below")
                traceback.print_exc()
        finally:
            # Guaranteed close on every exit path -- see _run_pipeline's
            # identical comment for why this must be a finally, not just
            # the except-Exception branch (KeyboardInterrupt is a
            # BaseException and would otherwise leak the connection).
            with contextlib.suppress(Exception):
                conn.close()

    # ── Recent runs ───────────────────────────────────────────────────────────

    def _show_runs(self) -> None:
        _section("Recent Runs")
        conn = self._open_db()
        if conn is None:
            return
        try:
            rows = conn.execute(
                """
                SELECT run_id, ts, note FROM events
                WHERE event_type='RUN_START'
                ORDER BY id DESC LIMIT 10
                """
            ).fetchall()
            if not rows:
                _info("No runs recorded yet.")
                return
            for row in rows:
                note = row["note"] or ""
                is_dry = "dry_run=True" in note
                mode = _c(" [DRY]", _DIM) if is_dry else ""
                print(f"    {_c(row['run_id'], _CYAN)}{mode}  {row['ts']}")
        finally:
            conn.close()

    # ── Run detail ────────────────────────────────────────────────────────────

    def _show_run_detail(self) -> None:
        conn = self._open_db()
        if conn is None:
            return
        try:
            # Show recent runs first so user can pick without going back to menu 4
            rows_recent = conn.execute(
                """
                SELECT run_id, ts, note FROM events
                WHERE event_type='RUN_START'
                ORDER BY id DESC LIMIT 10
                """
            ).fetchall()
            if rows_recent:
                _section("Recent Runs  (copy a Run ID below)")
                for row in rows_recent:
                    note = row["note"] or ""
                    is_dry = "dry_run=True" in note
                    mode = _c(" [DRY]", _DIM) if is_dry else ""
                    print(f"    {_c(row['run_id'], _CYAN)}{mode}  {row['ts']}")
                print()

            run_id = _prompt("Run ID (or partial)")
            if not run_id:
                return
            rows = conn.execute(
                """
                SELECT run_id, ts, event_type, file_path, stage, note
                FROM events
                WHERE run_id LIKE ?
                ORDER BY id
                LIMIT 200
                """,
                (f"%{run_id}%",),
            ).fetchall()
            if not rows:
                _warn(f"No events found for run_id ~= {run_id!r}")
                return
            _section(f"Events for {rows[0]['run_id']}")
            for row in rows:
                fp = f"  {Path(row['file_path']).name}" if row["file_path"] else ""
                stage = f" [{row['stage']}]" if row["stage"] else ""
                note = f"  {row['note']}" if row["note"] else ""
                print(f"    {_c(row['event_type'], _CYAN)}{stage}{fp}{_c(note, _DIM)}")
        finally:
            conn.close()

    # ── Duplicates viewer ─────────────────────────────────────────────────────

    def _show_duplicates(self) -> None:
        _section("Pending Duplicates")
        conn = self._open_db()
        if conn is None:
            return
        try:
            rows = conn.execute(
                """
                SELECT group_id, file_path, duplicate_type, confidence
                FROM duplicates
                WHERE status='pending'
                ORDER BY group_id, file_path
                LIMIT 100
                """
            ).fetchall()
            if not rows:
                _ok("No pending duplicates.")
                return
            current_group = None
            for row in rows:
                if row["group_id"] != current_group:
                    current_group = row["group_id"]
                    print(
                        f"\n    {_c(row['group_id'], _BOLD)}  "
                        f"{_c(row['duplicate_type'], _YELLOW)}  "
                        f"confidence={row['confidence']:.0%}"
                    )
                print(f"      {Path(row['file_path']).name}")
        finally:
            conn.close()

    # ── Config display ────────────────────────────────────────────────────────

    def _show_config(self) -> None:
        _section("Configuration")
        if self._config:
            for line in self._config.describe().splitlines():
                _info(line)
        else:
            _warn("Config not loaded.")

    # ── API keys ──────────────────────────────────────────────────────────────

    def _manage_api_keys(self) -> None:
        _section("Enter/Update API Keys")
        from .setup import run_api_key_manager

        run_api_key_manager()

        # Refresh so the Configuration view reflects any key that took
        # effect immediately (i.e. no higher-precedence env var was
        # already active for it). Keys that showed the shell-export
        # warning intentionally won't change here -- that's correct,
        # since MusicConfig.from_env() would see the same override.
        with contextlib.suppress(Exception):
            self._config = MusicConfig.from_env()

    # ── Reset / fresh-start ───────────────────────────────────────────────────

    def _reset_menu(self) -> None:
        _section("Reset / Fresh Start")
        _warn("This will mark all files for re-processing or wipe the database.")
        _info("Files in INBOX are never deleted — only the DB records change.")
        print()

        opts = [
            "Soft reset  — re-queue all CATALOGUED files (keeps event log, re-runs pipeline)",
            "Hard reset  — delete the entire database (true blank slate, irreversible)",
            "Back",
        ]
        choice = _choose("Reset mode", opts)
        try:
            idx = int(choice)
        except ValueError:
            return

        if idx == 2:
            return

        assert self._config is not None

        # ── Soft reset ────────────────────────────────────────────────────────
        if idx == 0:
            conn = self._open_db()
            if conn is None:
                return
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM archive WHERE status != 'PENDING'"
                ).fetchone()[0]
                _warn(f"This will mark {count:,} file(s) back to PENDING.")
                confirm = _prompt("Type YES to confirm")
                if confirm != "YES":
                    _info("Cancelled.")
                    return
                conn.execute(
                    """
                    UPDATE archive
                       SET status       = 'PENDING',
                           audio_hash   = NULL,
                           full_hash    = NULL,
                           rg_tagged_at = NULL
                    """
                )
                conn.execute(
                    """
                    INSERT INTO events (run_id, event_type, note)
                    VALUES ('manual', 'SOFT_RESET', 'console soft-reset: all files → PENDING')
                    """
                )
                conn.commit()
                _ok(f"Soft reset complete — {count:,} file(s) queued for re-processing.")
                _info("Run the full pipeline to re-hash and re-catalogue.")
            finally:
                conn.close()

        # ── Hard reset ────────────────────────────────────────────────────────
        elif idx == 1:
            db_path = self._config.db_path
            _warn(f"This will PERMANENTLY DELETE: {db_path}")
            _warn("All run history, hashes, metadata, and duplicate decisions will be lost.")
            confirm1 = _prompt("Type DELETE to confirm")
            if confirm1 != "DELETE":
                _info("Cancelled.")
                return
            confirm2 = _prompt("Type DELETE again to be sure")
            if confirm2 != "DELETE":
                _info("Cancelled.")
                return
            try:
                # Also remove WAL/SHM sidecar files
                for suffix in ("", "-wal", "-shm"):
                    p = db_path.parent / (db_path.name + suffix)
                    if p.exists():
                        p.unlink()
                _ok(f"Database deleted: {db_path}")
                _info("Restart musaeus to begin fresh — Ingest will re-register all inbox files.")
                # Stop the console — DB is gone
                self._running = False
            except OSError as exc:
                _err(f"Delete failed: {exc}")

    # ── Dedupe review ─────────────────────────────────────────────────────────

    def _run_dedupe(self) -> None:
        _section("Dedupe Review")
        conn = self._open_db()
        if conn is None:
            return
        try:
            opts = [
                "Interactive review  (manual keep/archive per group)",
                "Auto-resolve        (keep highest quality, archive rest)",
                "Report only         (show summary, no changes)",
                "Back",
            ]
            choice = _choose("Dedupe mode", opts)
            try:
                idx = int(choice)
            except ValueError:
                return
            from .dedupe import print_dedupe_report, run_dedupe_console
            if idx == 0:
                run_dedupe_console(conn, auto_mode=False)
            elif idx == 1:
                run_dedupe_console(conn, auto_mode=True)
            elif idx == 2:
                print_dedupe_report(conn)
        finally:
            conn.close()

    # ── Stage submenu ─────────────────────────────────────────────────────────

    def _run_stage_with_stash(
        self, stage_cls: type, dry_run: bool, stash: dict | None = None
    ) -> None:
        """Like _run_stage but pre-loads ctx stash keys (for curator, forge --force, etc.)."""
        mode = "DRY RUN" if dry_run else "LIVE RUN"
        assert self._config is not None
        conn = self._open_db()
        if conn is None:
            return
        try:
            try:
                ctx = RunContext.new(self._config, conn, dry_run=dry_run)
                if stash:
                    for k, v in stash.items():
                        ctx.set(k, v)
                stage = stage_cls()
                _section(f"{stage.NAME.upper()}  [{mode}]")
                result = stage.execute(ctx)
                if result.success:
                    _ok(result.summarise())
                else:
                    _err(result.summarise())
                for note in result.notes:
                    _info(note)
                for err_msg in result.errors:
                    _err(f"  ERROR: {err_msg}")
                ctx.finish()
            except Exception:
                _err("Stage crashed — see traceback below")
                traceback.print_exc()
        finally:
            # Guaranteed close on every exit path -- see _run_pipeline's
            # identical comment for why this must be a finally, not just
            # the except-Exception branch (KeyboardInterrupt is a
            # BaseException and would otherwise leak the connection).
            # This is the exact call site from the traceback that
            # exposed the bug (Forge failing with "database is locked"
            # after an interrupted Sentinel run leaked its connection).
            with contextlib.suppress(Exception):
                conn.close()

    def _stage_menu(self) -> None:
        stages: list[tuple[str, type, dict]] = [
            ("Ingest    — register new files from inbox",       IngestStage,   {}),
            ("Sentinel  — hash files + detect exact dupes",     SentinelStage, {}),
            ("Scholar   — extract ffprobe metadata",            ScholarStage,  {}),
            ("Forge     — measure LUFS + write ReplayGain tags", ForgeStage,  {}),
            ("Tagger    — write normalised DB metadata to files", TaggerStage, {}),
            ("Curator   — build car-library export",            CuratorStage,  {}),
            ("Playlists — build per-genre M3U8 playlists",     PlaylistStage, {}),
        ]
        opts = [label for label, _, _ in stages] + ["Back"]
        choice = _choose("Select stage", opts)
        try:
            idx = int(choice)
        except ValueError:
            return
        if idx >= len(stages):
            return
        label, cls, default_stash = stages[idx]

        stash: dict = dict(default_stash)

        # Curator needs an export root
        if cls is CuratorStage:
            export_raw = _prompt("Export root path (e.g. /mnt/USB)")
            if not export_raw:
                _warn("No export root — cancelled.")
                return
            stash["curator_export_root"] = Path(export_raw)
            noise_choice = _choose(
                "Noise profile",
                ["clean (none)", "pink", "brown", "white", "dual (pink+brown)  [default]"],
                default="4",
            )
            noise_map = {0: "clean", 1: "pink", 2: "brown", 3: "white", 4: "dual"}
            try:
                stash["curator_noise"] = noise_map.get(int(noise_choice), "dual")
            except ValueError:
                stash["curator_noise"] = "dual"

        mode_opts = ["Dry run  (preview only)", "Live run (real changes)", "Back"]
        mode_choice = _choose("Mode", mode_opts)
        try:
            mode_idx = int(mode_choice)
        except ValueError:
            return
        if mode_idx == 0:
            self._run_stage_with_stash(cls, dry_run=True, stash=stash)
        elif mode_idx == 1:
            self._run_stage_with_stash(cls, dry_run=False, stash=stash)

    # ── Main menu ─────────────────────────────────────────────────────────────

    def _main_menu(self) -> None:
        options = [
            ("Status",                        self._show_status),
            ("Run full pipeline  [DRY RUN]",  lambda: self._run_pipeline(dry_run=True)),
            ("Run full pipeline  [LIVE]",     lambda: self._run_pipeline(dry_run=False)),
            ("Run single stage…",             self._stage_menu),
            ("Dedupe review",                 self._run_dedupe),
            ("View recent runs",              self._show_runs),
            ("Inspect a run",                 self._show_run_detail),
            ("View duplicates",               self._show_duplicates),
            ("Configuration",                 self._show_config),
            ("Enter/Update API Keys",         self._manage_api_keys),
            ("Reset / fresh start",           self._reset_menu),
            ("Quit",                          self._quit),
        ]

        _header(f"MUSAEUS  v{self.VERSION}")
        choice = _choose("Select action", [label for label, _ in options])

        try:
            idx = int(choice)
        except ValueError:
            _warn(f"Invalid choice: {choice!r}")
            return

        if 0 <= idx < len(options):
            _, action = options[idx]
            try:
                action()
            except KeyboardInterrupt:
                print()
                _warn("Interrupted.")
        else:
            _warn(f"No option {idx}.")

    def _quit(self) -> None:
        print()
        _ok("Goodbye.")
        self._running = False

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the interactive console loop."""
        # Suppress library-level logging noise in console mode
        logging.basicConfig(
            level=logging.WARNING,
            format="%(levelname)s  %(name)s  %(message)s",
        )

        _header(f"MUSAEUS  v{self.VERSION}  —  Music Library Pipeline")
        print()
        _info("Musaeus — the student of Orpheus, keeper of sacred knowledge.")
        _info("Type a number and press Enter at each menu.")

        if not self._boot_check():
            sys.exit(1)

        while self._running:
            try:
                self._main_menu()
            except KeyboardInterrupt:
                print()
                _warn("Use option 11 (Quit) to exit cleanly.")
            except Exception:
                _err("Unexpected error in console loop:")
                traceback.print_exc()


def main() -> None:
    """Entry point for `musaeus console` and `python -m musaeus.console`."""
    Console().run()


if __name__ == "__main__":
    main()
