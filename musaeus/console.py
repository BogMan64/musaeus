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

import logging
import os
import sys
import textwrap
import traceback
from pathlib import Path
from typing import Callable

from .config import MusicConfig, get_config
from .context import RunContext
from .db import open_db
from .hasher import ffmpeg_available, ffprobe_available
from .stages import DEFAULT_PIPELINE, IngestStage, ScholarStage, SentinelStage
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

            _info(f"Total files   : {_c(str(total), _BOLD)}")
            _info(f"  PENDING     : {pending}")
            _info(f"  HASHED      : {hashed}")
            _info(f"  CATALOGUED  : {catalogued}")
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
            try:
                conn.close()
            except Exception:
                pass

    # ── Run single stage ──────────────────────────────────────────────────────

    def _run_stage(self, stage_cls: type[BaseStage], dry_run: bool) -> None:
        mode = "DRY RUN" if dry_run else "LIVE RUN"
        assert self._config is not None
        conn = self._open_db()
        if conn is None:
            return
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
            try:
                conn.close()
            except Exception:
                pass

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

    # ── Stage submenu ─────────────────────────────────────────────────────────

    def _stage_menu(self) -> None:
        stages = [
            ("Ingest  — register new files from inbox", IngestStage),
            ("Sentinel — hash files + detect exact dupes", SentinelStage),
            ("Scholar  — extract ffprobe metadata", ScholarStage),
        ]
        opts = [label for label, _ in stages] + ["Back"]
        choice = _choose("Select stage", opts)
        try:
            idx = int(choice)
        except ValueError:
            return
        if idx >= len(stages):
            return
        label, cls = stages[idx]
        mode_opts = ["Dry run  (preview only)", "Live run (real changes)", "Back"]
        mode_choice = _choose("Mode", mode_opts)
        try:
            mode_idx = int(mode_choice)
        except ValueError:
            return
        if mode_idx == 0:
            self._run_stage(cls, dry_run=True)
        elif mode_idx == 1:
            self._run_stage(cls, dry_run=False)

    # ── Main menu ─────────────────────────────────────────────────────────────

    def _main_menu(self) -> None:
        options = [
            ("Status",              self._show_status),
            ("Run full pipeline  [DRY RUN]",  lambda: self._run_pipeline(dry_run=True)),
            ("Run full pipeline  [LIVE]",      lambda: self._run_pipeline(dry_run=False)),
            ("Run single stage…",  self._stage_menu),
            ("View recent runs",   self._show_runs),
            ("Inspect a run",      self._show_run_detail),
            ("View duplicates",    self._show_duplicates),
            ("Configuration",      self._show_config),
            ("Quit",               self._quit),
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
                _warn("Use option 8 (Quit) to exit cleanly.")
            except Exception:
                _err("Unexpected error in console loop:")
                traceback.print_exc()


def main() -> None:
    """Entry point for `musaeus console` and `python -m musaeus.console`."""
    Console().run()


if __name__ == "__main__":
    main()
