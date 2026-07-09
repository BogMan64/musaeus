#!/usr/bin/env python3
"""
MUSAEUS — CLI entry point.

Usage:
    musaeus [command] [options]

Commands:
    run        Run the full pipeline (Ingest → Sentinel → Scholar)
    dry-run    Preview the full pipeline without any mutations
    ingest     Run Ingest stage only
    sentinel   Run Sentinel stage only
    scholar    Run Scholar stage only
    status     Show library status
    console    Launch the interactive console
    runs       List recent runs
    version    Print version

Options:
    --dry-run   Preview mode (no mutations) — applies to run, ingest, sentinel, scholar
    --verbose   Enable DEBUG logging

Examples:
    musaeus run
    musaeus run --dry-run
    musaeus ingest --dry-run
    musaeus status
    musaeus console
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from pathlib import Path

from . import __version__
from .config import get_config
from .context import RunContext
from .db import open_db
from .stages import DEFAULT_PIPELINE, IngestStage, ScholarStage, SentinelStage
from .stages.base import BaseStage


# ── Logging setup ─────────────────────────────────────────────────────────────

def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


# ── Pipeline runner ───────────────────────────────────────────────────────────

def _run_pipeline(stages: list[type[BaseStage]], dry_run: bool) -> int:
    """
    Run a sequence of stages.
    Returns 0 on success, 1 on any stage failure.
    """
    try:
        cfg = get_config()
        cfg.ensure_dirs()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    conn = open_db(cfg.db_path)
    ctx = RunContext.new(cfg, conn, dry_run=dry_run)
    mode = " [DRY RUN]" if dry_run else ""
    print(f"\nMusaeus pipeline{mode}  —  run_id={ctx.run_id}\n")

    exit_code = 0
    for cls in stages:
        stage = cls()
        result = stage.execute(ctx)
        status = "✓" if result.success else "✗"
        print(f"  {status}  {result.summarise()}")
        for note in result.notes:
            print(f"       {note}")
        for err in result.errors:
            print(f"       ERROR: {err}", file=sys.stderr)
        if not result.success:
            exit_code = 1

    print()
    all_ok = all(r.success for r in ctx.stage_results)
    if all_ok:
        print(f"  Pipeline complete.  run_id={ctx.run_id}")
    else:
        print(f"  Pipeline finished with errors.  run_id={ctx.run_id}", file=sys.stderr)

    ctx.finish()
    return exit_code


# ── Status command ────────────────────────────────────────────────────────────

def _cmd_status() -> int:
    try:
        cfg = get_config()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    conn = open_db(cfg.db_path)
    try:
        total      = conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
        pending    = conn.execute("SELECT COUNT(*) FROM archive WHERE status='PENDING'").fetchone()[0]
        hashed     = conn.execute("SELECT COUNT(*) FROM archive WHERE status='HASHED'").fetchone()[0]
        catalogued = conn.execute("SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'").fetchone()[0]
        dupes      = conn.execute("SELECT COUNT(DISTINCT group_id) FROM duplicates WHERE status='pending'").fetchone()[0]
        last_run   = conn.execute("SELECT MAX(ts) FROM events WHERE event_type='RUN_START'").fetchone()[0]
    finally:
        conn.close()

    print(f"\nMusaeus Library Status")
    print(f"  Vault      : {cfg.vault_root}")
    print(f"  DB         : {cfg.db_path}")
    print(f"  Total      : {total}")
    print(f"    PENDING  : {pending}")
    print(f"    HASHED   : {hashed}")
    print(f"    CATALOGUED: {catalogued}")
    print(f"  Dupes      : {dupes} group(s) pending")
    print(f"  Last run   : {last_run or 'never'}")

    from .config import AUDIO_EXTENSIONS
    inbox = cfg.inbox
    if inbox.exists():
        n = sum(1 for f in inbox.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS)
        print(f"  Inbox      : {n} audio file(s) in {inbox}")
    else:
        print(f"  Inbox      : NOT FOUND ({inbox})")
    print()
    return 0


# ── Runs command ──────────────────────────────────────────────────────────────

def _cmd_runs() -> int:
    try:
        cfg = get_config()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    conn = open_db(cfg.db_path)
    try:
        rows = conn.execute(
            """
            SELECT run_id, ts, note FROM events
            WHERE event_type='RUN_START'
            ORDER BY id DESC LIMIT 20
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No runs recorded yet.")
        return 0

    print(f"\nRecent Runs (last {len(rows)})")
    for row in rows:
        note = row["note"] or ""
        mode = " [DRY]" if "dry_run=True" in note else ""
        print(f"  {row['run_id']}{mode}  {row['ts']}")
    print()
    return 0


# ── Argument parser ───────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="musaeus",
        description="Musaeus — Music Library Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--version", action="version", version=f"musaeus {__version__}")
    p.add_argument("--verbose", "-v", action="store_true", help="Enable DEBUG logging")

    sub = p.add_subparsers(dest="command", metavar="command")

    # run
    run_p = sub.add_parser("run", help="Run full pipeline (Ingest→Sentinel→Scholar)")
    run_p.add_argument("--dry-run", action="store_true", help="Preview only, no mutations")

    # dry-run shortcut
    sub.add_parser("dry-run", help="Alias for: run --dry-run")

    # individual stages
    for name in ("ingest", "sentinel", "scholar"):
        sp = sub.add_parser(name, help=f"Run {name} stage only")
        sp.add_argument("--dry-run", action="store_true", help="Preview only")

    # status
    sub.add_parser("status", help="Show library status")

    # runs
    sub.add_parser("runs", help="List recent pipeline runs")

    # console
    sub.add_parser("console", help="Launch interactive console")

    # version
    sub.add_parser("version", help="Print version and exit")

    return p


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    _setup_logging(getattr(args, "verbose", False))

    command = args.command or "console"  # default to console if no command given
    dry_run = getattr(args, "dry_run", False)

    try:
        if command in ("run", None):
            sys.exit(_run_pipeline(DEFAULT_PIPELINE, dry_run=dry_run))

        elif command == "dry-run":
            sys.exit(_run_pipeline(DEFAULT_PIPELINE, dry_run=True))

        elif command == "ingest":
            sys.exit(_run_pipeline([IngestStage], dry_run=dry_run))

        elif command == "sentinel":
            sys.exit(_run_pipeline([SentinelStage], dry_run=dry_run))

        elif command == "scholar":
            sys.exit(_run_pipeline([ScholarStage], dry_run=dry_run))

        elif command == "status":
            sys.exit(_cmd_status())

        elif command == "runs":
            sys.exit(_cmd_runs())

        elif command == "console":
            from .console import Console
            Console().run()

        elif command == "version":
            print(f"musaeus {__version__}")

        else:
            parser.print_help()
            sys.exit(1)

    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
