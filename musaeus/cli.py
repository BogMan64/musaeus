#!/usr/bin/env python3
"""
MUSAEUS — CLI entry point.

Usage:
    musaeus [command] [options]

Pipeline commands:
    preflight        Environment checks (commands, packages, disk, DB) — report-only
    run              Run the default pipeline (Preflight → Ingest → Sentinel → Scholar)
    run --full       Run the full pipeline  (+ Normalize → Forge → Tagger)
    run --maintain   Run the maintenance pipeline (Ghost→Health→Normalize→Enrich→MBEnrich→NearDupe)
    run --enrich     Run the enrichment pipeline (Enrich→MBEnrich→AcousticID→Reviewer)
    dry-run          Preview the default pipeline without any mutations
    ingest           Run Ingest stage only
    sentinel         Run Sentinel stage only
    scholar          Run Scholar stage only
    normalize        Article-suffix fix + ALL-CAPS repair on archived metadata
    canonicalize     Lossless→ALAC / sub-lossless→AAC, both as .m4a (Act 3)
    finalize         Move canonicalized files INBOX → ALAC-Library (Act 3)
    audit            Physical-presence gate before DB snapshot+wipe (Act 3)
    cross-dupe       Flag files already in ALAC-Library from a prior batch (Act 2)
    forge            Measure EBU R128 loudness + write ReplayGain tags
    tagger           Write normalised DB metadata back to file tags
    auditor          Pre-forge LUFS audit (flags out-of-window files)
    curator          Build car-library export (requires --export-root)
    ghost            Sweep for archive entries missing from disk
    health           Run library consistency + quality checks
    enrich           Last.fm genre enrichment for tracks with missing genre
    mb-enrich        MusicBrainz artist + release MBID enrichment
    neardupe         Metadata-based near-duplicate detection
    acousticid       Acoustic fingerprint dedup via fpcalc + AcousticID API
    transcode        Lossless → 256k AAC export via ffmpeg
    reviewer         Groq AI metadata quality review
    report           Dashboard: library stats, genre/bitrate breakdown
    review-report    Show AI reviewer issues summary
    upgrade-check    Find lossy tracks where a lossless version exists

Review commands:
    dedupe           Interactive duplicate review console
    health-report    Show validation issues summary
    status           Show library status
    runs             List recent pipeline runs

Options:
    --dry-run        Preview mode (no mutations)
    --verbose / -v   Enable DEBUG logging
    --force          Re-process already-done files (forge, curator, transcode)
    --full           Include Forge + Tagger in `run` pipeline
    --maintain       Run Ghost + Health + Enrich + NearDupe in `run`
    --enrich         Run Enrich + MBEnrich + AcousticID + Reviewer in `run`
    --auto           Auto-resolve duplicates (dedupe command)
    --export-root    Target path for curator/transcode export
    --noise          Noise profile for curator: clean|pink|brown|white|dual
    --max-files      Cap files processed per run (auditor, reviewer)

Examples:
    musaeus run
    musaeus run --full
    musaeus run --maintain
    musaeus run --enrich
    musaeus forge --dry-run
    musaeus tagger
    musaeus curator --export-root /mnt/USB --noise dual
    musaeus ghost
    musaeus health
    musaeus normalize --dry-run
    musaeus auditor --dry-run
    musaeus enrich --dry-run
    musaeus mb-enrich --dry-run
    musaeus neardupe --dry-run
    musaeus acousticid --dry-run
    musaeus transcode --dry-run
    musaeus transcode --export-root /mnt/USB/AAC
    musaeus reviewer --dry-run
    musaeus reviewer --max-files 100
    musaeus report
    musaeus report --json
    musaeus review-report
    musaeus upgrade-check
    musaeus upgrade-check --csv
    musaeus dedupe
    musaeus dedupe --auto
    musaeus health-report
    musaeus status
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from pathlib import Path

from . import __version__
from .config import get_config
from .context import RunContext
from .db import open_db
from .stages import (
    DEFAULT_PIPELINE,
    ENRICH_PIPELINE,
    FULL_PIPELINE,
    ARCHIVE_PIPELINE,
    BIG_KAHUNA_PIPELINE,
    MAINTAIN_PIPELINE,
    AcousticIDStage,
    AlbumArtStage,
    AuditorStage,
    AuditStage,
    CuratorStage,
    EnrichStage,
    ForgeStage,
    GhostStage,
    HealthStage,
    IngestStage,
    IntegrityStage,
    MBEnrichStage,
    CanonicalizeStage,
    CrossDupeStage,
    FinalizeStage,
    NearDupeStage,
    NormalizeStage,
    OrganizeStage,
    PlaylistStage,
    PreflightStage,
    ReviewerStage,
    SanitizeStage,
    ScholarStage,
    SentinelStage,
    TaggerStage,
    TranscodeStage,
)
from .stages.base import BaseStage

# ── Logging setup ─────────────────────────────────────────────────────────────


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


# ── Resume state ──────────────────────────────────────────────────────────────
_RESUME_FILE = Path.home() / ".config" / "musaeus" / "resume_state.json"


def _save_resume(completed: list[str], all_stages: list[str]) -> None:
    _RESUME_FILE.parent.mkdir(parents=True, exist_ok=True)
    _RESUME_FILE.write_text(json.dumps(
        {"completed": completed, "all_stages": all_stages}, indent=2
    ))


def _load_resume(all_stages: list[str]) -> list[str] | None:
    if not _RESUME_FILE.exists():
        return None
    try:
        state = json.loads(_RESUME_FILE.read_text())
        if state.get("all_stages") != all_stages:
            return None
        completed = state.get("completed", [])
        return completed if completed else None
    except (json.JSONDecodeError, OSError):
        return None


def _clear_resume() -> None:
    try:
        _RESUME_FILE.unlink(missing_ok=True)
    except OSError:
        pass


# ── Pipeline runner ───────────────────────────────────────────────────────────


def _run_pipeline(
    stages: list[type[BaseStage]],
    dry_run: bool,
    stash: dict | None = None,
) -> int:
    """
    Run a sequence of stages.
    stash: optional dict of key→value to pre-load into ctx before running.
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
    if stash:
        for k, v in stash.items():
            ctx.set(k, v)

    mode = " [DRY RUN]" if dry_run else ""
    print(f"\nMusaeus pipeline{mode}  —  run_id={ctx.run_id}")

    # Show API key status hint
    missing_keys = []
    if not cfg.groq_api_key:
        missing_keys.append("Groq")
    if not cfg.lastfm_api_key:
        missing_keys.append("Last.fm")
    if not cfg.acousticid_api_key:
        missing_keys.append("AcousticID")
    if missing_keys:
        print(f"  ⚠ Missing API keys: {', '.join(missing_keys)} (run 'musaeus setup' to configure)")
    print()

    stage_names = [cls.__name__ for cls in stages]
    completed_names: list[str] = []

    # Check for resume
    resume_from = _load_resume(stage_names)
    if resume_from:
        print(f"  ⚠  Incomplete run detected — {len(resume_from)} stage(s) done.")
        if not sys.stdin.isatty():
            # No TTY (cron, background process, piped/redirected shell) —
            # a bare input() here would block forever with no way to answer.
            # Auto-resume is the safe default: it's exactly what the [Y]
            # default in the interactive prompt below would do anyway.
            print("  ⚠  No TTY detected — auto-resuming non-interactively.")
            completed_names = list(resume_from)
        else:
            try:
                answer = input("  Resume from next stage? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if answer == "n":
                _clear_resume()
            else:
                completed_names = list(resume_from)

    exit_code = 0
    for cls in stages:
        stage_name = cls.__name__
        if stage_name in completed_names:
            print(f"  ⏭  {stage_name} (already done)")
            continue

        stage = cls()
        try:
            result = stage.execute(ctx)
        except KeyboardInterrupt:
            print(f"\n\n  ⚠  Interrupted during {stage_name}.")
            print("  Progress saved — run 'musaeus run' again to resume.\n")
            _save_resume(completed_names, stage_names)
            return 1

        status = "✓" if result.success else "✗"
        print(f"  {status}  {result.summarise()}")
        for note in result.notes:
            print(f"       {note}")
        for err in result.errors:
            print(f"       ERROR: {err}", file=sys.stderr)

        completed_names.append(stage_name)
        _save_resume(completed_names, stage_names)

        if not result.success:
            exit_code = 1

    print()
    all_ok = all(r.success for r in ctx.stage_results)
    if all_ok:
        _clear_resume()
        print(f"  Pipeline complete.  run_id={ctx.run_id}")
    else:
        print(f"  Pipeline finished with errors.  run_id={ctx.run_id}", file=sys.stderr)

    ctx.finish()
    return exit_code


# ── Reset command ─────────────────────────────────────────────────────────────


def _cmd_reset() -> None:
    """Wipe the MUSAEUS database for a completely fresh start."""
    try:
        cfg = get_config()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return

    db = cfg.db_path
    print("\n  MUSAEUS — Database Reset")
    print(f"  DB: {db}")
    print(f"  This will DELETE the database and all pipeline state.")
    print(f"  Your music files in the vault are NOT affected.")
    print()

    if not sys.stdin.isatty():
        # No TTY (cron, background process, piped/redirected shell) —
        # a bare input() here would block forever. Unlike the resume
        # prompt, this is destructive (wipes the DB), so the safe default
        # is to refuse, not to auto-confirm.
        print("  ⚠  No TTY detected — refusing to reset non-interactively.")
        print("  Run this command from an interactive shell to confirm.")
        return

    try:
        confirm = input("  Type RESET to confirm: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.")
        return

    if confirm != "RESET":
        print("  Cancelled.")
        return

    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            p.unlink()
            print(f"  ✓ Deleted: {p.name}")

    # Also clear resume state
    _clear_resume()

    print("\n  ✓ Database reset complete.")
    print("  Run 'musaeus run' to re-ingest your library from the inbox.")
    print()


# ── Status command ────────────────────────────────────────────────────────────


def _cmd_status() -> int:
    try:
        cfg = get_config()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    conn = open_db(cfg.db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM archive WHERE status='PENDING'").fetchone()[0]
        hashed = conn.execute("SELECT COUNT(*) FROM archive WHERE status='HASHED'").fetchone()[0]
        catalogued = conn.execute(
            "SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'"
        ).fetchone()[0]
        forged = conn.execute(
            "SELECT COUNT(*) FROM archive WHERE rg_tagged_at IS NOT NULL"
        ).fetchone()[0]
        tagged_car = conn.execute(
            "SELECT COUNT(*) FROM archive WHERE car_export_path IS NOT NULL"
        ).fetchone()[0]
        dupes = conn.execute(
            "SELECT COUNT(DISTINCT group_id) FROM duplicates WHERE status='pending'"
        ).fetchone()[0]
        last_run = conn.execute(
            "SELECT MAX(ts) FROM events WHERE event_type='RUN_START'"
        ).fetchone()[0]
    finally:
        conn.close()

    print("\nMusaeus Library Status")
    print(f"  Vault       : {cfg.vault_root}")
    print(f"  DB          : {cfg.db_path}")
    print(f"  Total       : {total:,}")
    print(f"    PENDING   : {pending:,}")
    print(f"    HASHED    : {hashed:,}")
    print(f"    CATALOGUED: {catalogued:,}")
    print(f"    FORGED    : {forged:,}  (RG tagged)")
    print(f"    CAR EXPORT: {tagged_car:,}")
    print(f"  Dupes       : {dupes} group(s) pending")
    print(f"  Last run    : {last_run or 'never'}")

    from .config import AUDIO_EXTENSIONS

    inbox = cfg.inbox
    if inbox.exists():
        n = sum(1 for f in inbox.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS)
        print(f"  Inbox       : {n} audio file(s) in {inbox}")
    else:
        print(f"  Inbox       : NOT FOUND ({inbox})")
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


# ── Dedupe command ────────────────────────────────────────────────────────────


def _cmd_dedupe(auto: bool = False, report_only: bool = False) -> int:
    try:
        cfg = get_config()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    conn = open_db(cfg.db_path)
    try:
        from .dedupe import print_dedupe_report, run_dedupe_console

        if report_only:
            print_dedupe_report(conn)
        else:
            run_dedupe_console(conn, auto_mode=auto)
    finally:
        conn.close()
    return 0


# ── Health report command ─────────────────────────────────────────────────────


def _cmd_health_report() -> int:
    try:
        cfg = get_config()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    conn = open_db(cfg.db_path)
    try:
        # Overall counts
        total_issues = conn.execute("SELECT COUNT(*) FROM validation_issues").fetchone()[0]
        error_count = conn.execute(
            "SELECT COUNT(*) FROM validation_issues WHERE severity='error'"
        ).fetchone()[0]
        warn_count = conn.execute(
            "SELECT COUNT(*) FROM validation_issues WHERE severity='warning'"
        ).fetchone()[0]

        # Issue breakdown
        rows = conn.execute(
            """
            SELECT issue, severity, COUNT(*) as cnt
            FROM validation_issues
            GROUP BY issue, severity
            ORDER BY severity DESC, cnt DESC
            """
        ).fetchall()

        # Files with most issues
        bad_files = conn.execute(
            """
            SELECT file_path, COUNT(*) as cnt
            FROM validation_issues
            GROUP BY file_path
            ORDER BY cnt DESC
            LIMIT 10
            """
        ).fetchall()
    finally:
        conn.close()

    print("\nMusaeus Health Report")
    print(f"  Total issues : {total_issues:,}")
    print(f"    Errors     : {error_count:,}")
    print(f"    Warnings   : {warn_count:,}")

    if rows:
        print("\n  Issue breakdown:")
        for row in rows:
            sev_icon = "✗" if row["severity"] == "error" else "⚠"
            print(f"    {sev_icon} {row['issue']:<28} {row['cnt']:>6}")

    if bad_files:
        print("\n  Files with most issues (top 10):")
        for row in bad_files:
            from pathlib import Path

            print(f"    [{row['cnt']}] {Path(row['file_path']).name}")

    print()
    return 0


# ── Review report command ─────────────────────────────────────────────────────


def _cmd_review_report() -> int:
    try:
        cfg = get_config()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    conn = open_db(cfg.db_path)
    try:
        try:
            total = conn.execute("SELECT COUNT(*) FROM review_issues").fetchone()[0]
        except Exception:
            print("\nNo review_issues table yet — run `musaeus reviewer` first.")
            return 0

        rows = conn.execute(
            """
            SELECT issue_type, COUNT(*) as cnt,
                   AVG(confidence) as avg_conf
            FROM review_issues
            GROUP BY issue_type
            ORDER BY cnt DESC
            """
        ).fetchall()

        recent = conn.execute(
            """
            SELECT ri.issue_type, ri.detail, ri.confidence,
                   a.artist, a.title
            FROM review_issues ri
            LEFT JOIN archive a ON a.file_path = ri.file_path
            ORDER BY ri.id DESC
            LIMIT 20
            """
        ).fetchall()
    finally:
        conn.close()

    print("\nMusaeus Review Report")
    print(f"  Total issues : {total:,}")

    if rows:
        print("\n  By issue type:")
        for row in rows:
            print(
                f"    {row['issue_type']:<28}  "
                f"{row['cnt']:>5}  "
                f"(avg confidence {row['avg_conf']:.0%})"
            )

    if recent:
        print("\n  Most recent issues (up to 20):")
        for row in recent:
            artist = row["artist"] or "?"
            title = row["title"] or "?"
            print(
                f"    [{row['issue_type']}] {artist} — {title}  "
                f"({row['confidence']:.0%})  {row['detail'] or ''}"
            )

    print()
    return 0


# ── DB-tune command ───────────────────────────────────────────────────────────


def _cmd_db_tune() -> int:
    try:
        cfg = get_config()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    import os

    conn = open_db(cfg.db_path)
    try:
        # Enable WAL mode
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA mmap_size=268435456")  # 256 MB
        conn.commit()

        # Stats before
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        wal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

        print("\nMusaeus DB Tune")
        print(f"  DB        : {cfg.db_path}")
        print(f"  Size      : {os.path.getsize(cfg.db_path) / 1024 / 1024:.1f} MB")
        print(f"  Pages     : {page_count:,}  (page size: {page_size})")
        print(
            f"  Freelist  : {freelist:,} pages ({freelist * page_size / 1024:.0f} KB recoverable)"
        )
        print(f"  WAL mode  : {wal_mode}")

        print("\n  Running ANALYZE... ", end="", flush=True)
        conn.execute("ANALYZE")
        conn.commit()
        print("done.")

        if freelist > 100:
            print(f"  Running VACUUM ({freelist} free pages)... ", end="", flush=True)
            conn.execute("VACUUM")
            conn.commit()
            new_size = os.path.getsize(cfg.db_path)
            print(f"done.  New size: {new_size / 1024 / 1024:.1f} MB")
        else:
            print("  VACUUM skipped (freelist < 100 pages — DB is clean).")

        # Archive row count
        total = conn.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
        print(f"\n  Archive rows: {total:,}")
        print("  DB tuning complete.\n")
    finally:
        conn.close()
    return 0


# ── Upgrade check command ─────────────────────────────────────────────────────


def _cmd_upgrade_check(write_csv: bool = False, min_gap: int = 0) -> int:
    try:
        cfg = get_config()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    conn = open_db(cfg.db_path)
    try:
        from scripts.musaeus_upgrade_check import _analyse, _gather, _print_report

        rows = _gather(conn)
    finally:
        conn.close()

    candidates = _analyse(rows)
    _print_report(candidates, cfg, write_csv=write_csv, min_gap=min_gap)
    return 0


# ── Spec scout command ────────────────────────────────────────────────────────


def _cmd_spec_scout(write_csv: bool = False, min_bitrate: int = 0, max_bitrate: int = 0) -> int:
    try:
        cfg = get_config()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    conn = open_db(cfg.db_path)
    try:
        from scripts.musaeus_spec_scout import _analyse, _gather, _print_report

        rows = _gather(conn)
    finally:
        conn.close()

    print(f"Loaded {len(rows):,} catalogued tracks.")
    issues = _analyse(rows, min_bitrate=min_bitrate, max_bitrate=max_bitrate)
    _print_report(issues, cfg, write_csv=write_csv)
    return 0


# ── Canon review command ──────────────────────────────────────────────────────


def _cmd_canon_review(
    mode: str,
    csv_path: str | None = None,
    fixes_path: str | None = None,
    dry_run: bool = False,
) -> int:
    try:
        cfg = get_config()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    conn = open_db(cfg.db_path)
    try:
        from scripts.musaeus_canon_review import _apply, _report

        if mode == "report":
            cp = Path(csv_path) if csv_path else None
            _report(conn, cfg, cp)
        elif mode == "apply":
            if not fixes_path:
                print("ERROR: --fixes required for apply mode", file=sys.stderr)
                return 1
            _apply(conn, cfg, Path(fixes_path), dry_run=dry_run)
        else:
            print(f"ERROR: unknown canon-review mode '{mode}'", file=sys.stderr)
            return 1
    finally:
        conn.close()
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
    p.add_argument("--verbose", "-v", action="store_true", help="Enable DEBUG logging + detailed metrics")
    p.add_argument("--progress", action="store_true", default=True, help="Show progress bars (default: enabled)")
    p.add_argument("--no-progress", dest="progress", action="store_false", help="Disable progress bars")

    sub = p.add_subparsers(dest="command", metavar="command")

    # ── run ──────────────────────────────────────────────────────────────────
    run_p = sub.add_parser("run", help="Run pipeline (Ingest→Sentinel→Scholar)")
    run_p.add_argument("--dry-run", action="store_true", help="Preview only, no mutations")
    run_p.add_argument("--full", action="store_true", help="Also run Forge + Tagger stages")
    run_p.add_argument(
        "--archive",
        action="store_true",
        help="Everything minus LUFS: Ingest→Scholar→Normalize→Health→Enrich→Dedup→Art→Tagger",
    )
    run_p.add_argument(
        "--big-kahuna",
        action="store_true",
        help="ALL stages: full pipeline + health + enrich + dedup + art + curator + playlists",
    )
    run_p.add_argument(
        "--maintain", action="store_true", help="Run Ghost + Health + Enrich + NearDupe"
    )
    run_p.add_argument(
        "--enrich",
        action="store_true",
        help="Run Enrich + MBEnrich + AcousticID + Reviewer pipeline",
    )
    run_p.add_argument(
        "--reset",
        action="store_true",
        help="Clear resume state and start fresh",
    )

    # dry-run shortcut
    sub.add_parser("dry-run", help="Alias for: run --dry-run")

    # preflight
    preflight_p = sub.add_parser(
        "preflight", help="Environment checks (commands, packages, disk, DB) — report-only"
    )
    preflight_p.add_argument("--dry-run", action="store_true", help="Same as run (report-only, never mutates)")

    # ── setup wizard ──────────────────────────────────────────────────────────
    sub.add_parser("setup", help="Run the setup wizard (paths + API keys)")
    sub.add_parser("reset", help="Wipe DB for a fresh start (confirms before deleting)")

    # ── individual stages ─────────────────────────────────────────────────────
    for name in ("ingest", "sentinel", "scholar"):
        sp = sub.add_parser(name, help=f"Run {name} stage only")
        sp.add_argument("--dry-run", action="store_true", help="Preview only")

    # cross-dupe
    cross_dupe_p = sub.add_parser(
        "cross-dupe", help="Flag files matching ALAC-Library content from a prior batch"
    )
    cross_dupe_p.add_argument("--dry-run", action="store_true", help="Report matches, no DB writes")

    # normalize
    normalize_p = sub.add_parser("normalize", help="Article-suffix fix + ALL-CAPS repair")
    normalize_p.add_argument("--dry-run", action="store_true", help="Preview only, no DB changes")

    # organize
    organize_p = sub.add_parser("organize", help="Rename and reorganize files into Artist/Album/ structure")
    organize_p.add_argument("--dry-run", action="store_true", help="Preview only, no file moves")

    # sanitize
    sanitize_p = sub.add_parser("sanitize", help="Filesystem-safe metadata (Windows/ExFAT/Android)")
    sanitize_p.add_argument("--dry-run", action="store_true", help="Preview only, no DB changes")

    # canonicalize
    canonicalize_p = sub.add_parser(
        "canonicalize",
        help="Lossless->ALAC / sub-lossless->AAC, both as .m4a, based on real codec",
    )
    canonicalize_p.add_argument("--dry-run", action="store_true", help="Report actions, no ffmpeg calls")
    canonicalize_p.add_argument("--force", action="store_true", help="Re-process already-canonicalized files")

    # finalize
    finalize_p = sub.add_parser(
        "finalize", help="Move canonicalized files from INBOX into vault_root/ALAC-Library"
    )
    finalize_p.add_argument("--dry-run", action="store_true", help="Report moves, no files written")
    finalize_p.add_argument("--force", action="store_true", help="Re-process already-finalized files")

    # audit
    audit_p = sub.add_parser(
        "audit", help="Physical-presence verification gate before DB snapshot+wipe"
    )
    audit_p.add_argument("--dry-run", action="store_true", help="Same as run -- audit is inherently read-only")

    # forge
    forge_p = sub.add_parser("forge", help="Measure LUFS + write ReplayGain tags")
    forge_p.add_argument("--dry-run", action="store_true", help="Measure but don't write tags")
    forge_p.add_argument("--force", action="store_true", help="Re-tag already-forged files")
    forge_p.add_argument(
        "--target-lufs",
        type=float,
        default=None,
        metavar="LUFS",
        help="ReplayGain reference level in LUFS (default: -18.0). "
        "Common values: -18 (home), -16 (Apple Music), -14 (car/Spotify).",
    )

    # tagger
    tagger_p = sub.add_parser("tagger", help="Write normalised DB metadata back to file tags")
    tagger_p.add_argument("--dry-run", action="store_true", help="Preview only")

    # ghost
    ghost_p = sub.add_parser("ghost", help="Sweep archive for files missing from disk")
    ghost_p.add_argument("--dry-run", action="store_true", help="Report only, no DB changes")

    # health
    health_p = sub.add_parser("health", help="Library consistency and quality checks")
    health_p.add_argument("--dry-run", action="store_true", help="Report only, no DB writes")

    # corrupt
    corrupt_p = sub.add_parser("corrupt", help="Detect and quarantine corrupt/truncated audio files")
    corrupt_p.add_argument("--dry-run", action="store_true", help="Report only, no quarantine")

    # artist-consolidate
    artist_consol_p = sub.add_parser("artist-consolidate", help="Normalize artist name variants to canonical forms")
    artist_consol_p.add_argument("--dry-run", action="store_true", help="Show what would change, no DB writes")

    # auditor
    auditor_p = sub.add_parser("auditor", help="Pre-forge LUFS audit — flag out-of-window files")
    auditor_p.add_argument("--dry-run", action="store_true", help="Measure + report, no DB writes")
    auditor_p.add_argument(
        "--target-lufs",
        type=float,
        default=None,
        metavar="LUFS",
        help="Target integrated LUFS (default: -18.0)",
    )
    auditor_p.add_argument(
        "--max-files",
        type=int,
        default=None,
        metavar="N",
        help="Cap files per run (default: 200; 0 = no cap)",
    )

    # enrich
    enrich_p = sub.add_parser("enrich", help="Last.fm genre enrichment for missing genres")
    enrich_p.add_argument("--dry-run", action="store_true", help="Show what would change")

    # mb-enrich
    mb_p = sub.add_parser("mb-enrich", help="MusicBrainz artist + release MBID enrichment")
    mb_p.add_argument("--dry-run", action="store_true", help="Show what would change, no DB writes")

    # neardupe
    neardupe_p = sub.add_parser("neardupe", help="Metadata-based near-duplicate detection")
    neardupe_p.add_argument("--dry-run", action="store_true", help="Show matches without staging")

    # report
    report_p = sub.add_parser("report", help="Dashboard: library stats, genre/bitrate breakdown")
    report_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    report_p.add_argument("--wide", action="store_true", help="Use wider terminal columns (100)")

    # health-report
    sub.add_parser("health-report", help="Print validation issues summary")

    # rebuild-db
    rebuild_p = sub.add_parser("rebuild-db", help="Rebuild archive table from event log")
    rebuild_p.add_argument("--dry-run", action="store_true", help="Preview only")

    # curator
    curator_p = sub.add_parser("curator", help="Build car-library export")
    curator_p.add_argument("--export-root", metavar="PATH", help="Destination directory for export")
    curator_p.add_argument(
        "--noise",
        choices=["clean", "pink", "brown", "white", "dual"],
        default="dual",
        help="Noise profile to include (default: dual = pink+brown)",
    )
    curator_p.add_argument("--dry-run", action="store_true", help="Preview only")
    curator_p.add_argument("--force", action="store_true", help="Re-copy already-exported files")

    # acousticid
    acousticid_p = sub.add_parser(
        "acousticid", help="Acoustic fingerprint dedup via fpcalc + AcousticID API"
    )
    acousticid_p.add_argument(
        "--dry-run", action="store_true", help="Fingerprint + report, no DB writes"
    )

    # transcode
    transcode_p = sub.add_parser("transcode", help="Lossless → 256k AAC export via ffmpeg")
    transcode_p.add_argument(
        "--dry-run", action="store_true", help="Report what would be transcoded"
    )
    transcode_p.add_argument("--force", action="store_true", help="Re-transcode already-done files")
    transcode_p.add_argument(
        "--export-root",
        metavar="PATH",
        help="Output directory for transcoded files (default: vault/Transcoded)",
    )

    # reviewer
    reviewer_p = sub.add_parser("reviewer", help="Groq AI metadata quality review")
    reviewer_p.add_argument("--dry-run", action="store_true", help="Show what would be reviewed")
    reviewer_p.add_argument(
        "--max-files",
        type=int,
        default=None,
        metavar="N",
        help="Cap tracks reviewed per run (default: 50)",
    )

    # review-report
    sub.add_parser("review-report", help="Show AI reviewer issues summary")

    # upgrade-check
    upgrade_p = sub.add_parser(
        "upgrade-check", help="Find lossy tracks where a lossless version exists"
    )
    upgrade_p.add_argument("--csv", action="store_true", help="Also write CSV report")
    upgrade_p.add_argument(
        "--min-gap",
        type=int,
        default=0,
        metavar="KBPS",
        help="Only report if lossless exceeds lossy by at least N kbps",
    )

    # integrity
    integrity_p = sub.add_parser(
        "integrity", help="Detect corrupt/truncated files via ffprobe decode-test"
    )
    integrity_p.add_argument("--dry-run", action="store_true", help="Count, no DB writes")
    integrity_p.add_argument(
        "--max-files",
        type=int,
        default=None,
        metavar="N",
        help="Cap files checked per run (default: no cap)",
    )

    # albumart
    albumart_p = sub.add_parser(
        "albumart", help="Audit missing embedded art + embed sidecar images"
    )
    albumart_p.add_argument("--dry-run", action="store_true", help="Audit only, no embedding")
    albumart_p.add_argument("--force", action="store_true", help="Re-check all files")
    albumart_p.add_argument(
        "--no-embed", action="store_true", help="Audit only, skip sidecar embedding"
    )

    # overnight
    overnight_p = sub.add_parser(
        "overnight",
        help="Automated nightly self-heal: Ghost→Health→Normalize→Enrich→MBEnrich→NearDupe→Reviewer",
    )
    overnight_p.add_argument("--dry-run", action="store_true", help="Preview only")

    # playlist
    playlist_p = sub.add_parser(
        "playlist",
        help="Build M3U8 playlists (genre + All) with relative paths — works on Android & Apple",
    )
    playlist_p.add_argument("--dry-run", action="store_true", help="Preview only, no files written")

    # db-tune
    sub.add_parser("db-tune", help="VACUUM, ANALYZE, WAL mode + DB stats")

    # spec-scout
    spec_p = sub.add_parser(
        "spec-scout", help="Find audio spec outliers (bitrate/codec/sample-rate)"
    )
    spec_p.add_argument("--csv", action="store_true", help="Also write CSV report")
    spec_p.add_argument("--min-bitrate", type=int, default=0, metavar="KBPS")
    spec_p.add_argument("--max-bitrate", type=int, default=0, metavar="KBPS")

    # canon-review
    canon_p = sub.add_parser(
        "canon-review", help="Audit genre/artist/album canons and apply approved fixes"
    )
    canon_sub = canon_p.add_subparsers(dest="canon_mode", metavar="mode")
    canon_rep = canon_sub.add_parser("report", help="Generate canon review report")
    canon_rep.add_argument("--csv", metavar="PATH", help="Also write CSV")
    canon_appl = canon_sub.add_parser("apply", help="Apply approved fixes from CSV")
    canon_appl.add_argument("--fixes", required=True, metavar="PATH")
    canon_appl.add_argument("--dry-run", action="store_true")

    # ── review commands ───────────────────────────────────────────────────────

    # review (subcommand group)
    review_p = sub.add_parser("review", help="Album/artist review & approval workflow")
    review_sub = review_p.add_subparsers(dest="review_command", metavar="action")
    review_gen = review_sub.add_parser("generate", help="Generate review sheets from archive")
    review_gen.add_argument("--dry-run", action="store_true", help="Preview only")
    review_apply = review_sub.add_parser("apply", help="Apply approved fixes from review sheets")
    review_apply.add_argument("--dry-run", action="store_true", help="Preview only")
    review_sub.add_parser("status", help="Show pending review sheet status")

    # dedupe
    dedupe_p = sub.add_parser("dedupe", help="Interactive duplicate review console")
    dedupe_p.add_argument("--auto", action="store_true", help="Auto-resolve: keep highest quality")
    dedupe_p.add_argument(
        "--report", action="store_true", help="Show report only, no interactive review"
    )

    # status
    sub.add_parser("status", help="Show library status")

    # runs
    sub.add_parser("runs", help="List recent pipeline runs")

    # console (legacy interactive)
    sub.add_parser("console", help="Launch interactive console")

    # version
    sub.add_parser("version", help="Print version and exit")

    return p


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    verbose = getattr(args, "verbose", False)
    show_progress = getattr(args, "progress", True)  # Default to True
    
    _setup_logging(verbose)
    
    # Enable progress tracking if requested
    if verbose:
        from .progress import enable_verbose_logging
        enable_verbose_logging()

    command = args.command or "console"
    dry_run = getattr(args, "dry_run", False)

    # ── First-run check: trigger wizard if no config exists ───────────────────
    from .setup import needs_setup, run_wizard as _run_wizard

    if command == "setup":
        _run_wizard(force=True)
        return

    if command == "reset":
        _cmd_reset()
        return

    if needs_setup() and command not in ("setup", "reset", "status", "runs"):
        print("\n  Welcome to MUSAEUS! No configuration found.")
        print("  Running first-time setup wizard...\n")
        if not _run_wizard():
            return

    try:
        # Store progress settings in global state for pipeline runner
        import os
        os.environ["MUSAEUS_VERBOSE"] = "1" if verbose else "0"
        os.environ["MUSAEUS_PROGRESS"] = "1" if show_progress else "0"
        
        # ── pipeline commands ─────────────────────────────────────────────────

        if command == "run":
            if getattr(args, "reset", False):
                _clear_resume()
                print("  ✓ Resume state cleared.")
            if getattr(args, "maintain", False):
                pipeline = MAINTAIN_PIPELINE
            elif getattr(args, "big_kahuna", False):
                pipeline = BIG_KAHUNA_PIPELINE
            elif getattr(args, "full", False):
                pipeline = FULL_PIPELINE
            elif getattr(args, "archive", False):
                pipeline = ARCHIVE_PIPELINE
            elif getattr(args, "enrich", False):
                pipeline = ENRICH_PIPELINE
            else:
                pipeline = DEFAULT_PIPELINE
            sys.exit(_run_pipeline(pipeline, dry_run=dry_run))

        elif command == "dry-run":
            sys.exit(_run_pipeline(DEFAULT_PIPELINE, dry_run=True))

        elif command == "preflight":
            sys.exit(_run_pipeline([PreflightStage], dry_run=dry_run))

        elif command == "ingest":
            sys.exit(_run_pipeline([IngestStage], dry_run=dry_run))

        elif command == "sentinel":
            sys.exit(_run_pipeline([SentinelStage], dry_run=dry_run))

        elif command == "cross-dupe":
            sys.exit(_run_pipeline([CrossDupeStage], dry_run=dry_run))

        elif command == "scholar":
            sys.exit(_run_pipeline([ScholarStage], dry_run=dry_run))

        elif command == "normalize":
            sys.exit(_run_pipeline([NormalizeStage], dry_run=dry_run))

        elif command == "organize":
            sys.exit(_run_pipeline([OrganizeStage], dry_run=dry_run))

        elif command == "sanitize":
            sys.exit(_run_pipeline([SanitizeStage], dry_run=dry_run))

        elif command == "canonicalize":
            stash: dict = {}
            if getattr(args, "force", False):
                stash["canonicalize_force"] = True
            sys.exit(_run_pipeline([CanonicalizeStage], dry_run=dry_run, stash=stash))

        elif command == "finalize":
            stash = {}
            if getattr(args, "force", False):
                stash["finalize_force"] = True
            sys.exit(_run_pipeline([FinalizeStage], dry_run=dry_run, stash=stash))

        elif command == "audit":
            sys.exit(_run_pipeline([AuditStage], dry_run=dry_run))

        elif command == "forge":
            stash: dict = {}
            if getattr(args, "force", False):
                stash["forge_force"] = True
            target_lufs = getattr(args, "target_lufs", None)
            if target_lufs is not None:
                stash["forge_target_lufs"] = float(target_lufs)
            sys.exit(_run_pipeline([ForgeStage], dry_run=dry_run, stash=stash))

        elif command == "tagger":
            sys.exit(_run_pipeline([TaggerStage], dry_run=dry_run))

        elif command == "ghost":
            sys.exit(_run_pipeline([GhostStage], dry_run=dry_run))

        elif command == "health":
            sys.exit(_run_pipeline([HealthStage], dry_run=dry_run))

        elif command == "corrupt":
            from .stages import CorruptStage
            sys.exit(_run_pipeline([CorruptStage], dry_run=dry_run))

        elif command == "artist-consolidate":
            from .stages import ArtistConsolidateStage
            sys.exit(_run_pipeline([ArtistConsolidateStage], dry_run=dry_run))

        elif command == "auditor":
            stash: dict = {}
            tl = getattr(args, "target_lufs", None)
            if tl is not None:
                stash["auditor_target_lufs"] = float(tl)
            mf = getattr(args, "max_files", None)
            if mf is not None:
                stash["auditor_max_files"] = int(mf)
            sys.exit(_run_pipeline([AuditorStage], dry_run=dry_run, stash=stash))

        elif command == "enrich":
            sys.exit(_run_pipeline([EnrichStage], dry_run=dry_run))

        elif command == "mb-enrich":
            sys.exit(_run_pipeline([MBEnrichStage], dry_run=dry_run))

        elif command == "neardupe":
            sys.exit(_run_pipeline([NearDupeStage], dry_run=dry_run))

        elif command == "report":
            import json as _json

            from scripts.musaeus_report import _gather, _print_report

            cfg = get_config()
            conn = open_db(cfg.db_path)
            try:
                data = _gather(conn)
            finally:
                conn.close()
            if getattr(args, "json", False):
                print(_json.dumps(data, indent=2, default=str))
            else:
                _print_report(data, cfg, wide=getattr(args, "wide", False))
            sys.exit(0)

        elif command == "health-report":
            sys.exit(_cmd_health_report())

        elif command == "rebuild-db":
            from .rebuild import cmd_rebuild_db
            sys.exit(cmd_rebuild_db(dry_run=dry_run))

        elif command == "review":
            from .approval import cmd_review_generate, cmd_review_apply, cmd_review_status
            review_cmd = getattr(args, "review_command", None)
            if review_cmd == "generate":
                sys.exit(cmd_review_generate(dry_run=dry_run))
            elif review_cmd == "apply":
                sys.exit(cmd_review_apply(dry_run=dry_run))
            elif review_cmd == "status":
                sys.exit(cmd_review_status())
            else:
                print("Usage: musaeus review {generate|apply|status}")
                sys.exit(1)

        elif command == "curator":
            stash = {}
            export_root = getattr(args, "export_root", None)
            if export_root:
                stash["curator_export_root"] = Path(export_root)
            stash["curator_noise"] = getattr(args, "noise", "dual")
            if getattr(args, "force", False):
                stash["curator_force"] = True
            sys.exit(_run_pipeline([CuratorStage], dry_run=dry_run, stash=stash))

        elif command == "acousticid":
            sys.exit(_run_pipeline([AcousticIDStage], dry_run=dry_run))

        elif command == "transcode":
            stash = {}
            export_root = getattr(args, "export_root", None)
            if export_root:
                stash["transcode_root"] = Path(export_root)
            if getattr(args, "force", False):
                stash["transcode_force"] = True
            sys.exit(_run_pipeline([TranscodeStage], dry_run=dry_run, stash=stash))

        elif command == "reviewer":
            stash = {}
            mf = getattr(args, "max_files", None)
            if mf is not None:
                stash["reviewer_max_files"] = int(mf)
            sys.exit(_run_pipeline([ReviewerStage], dry_run=dry_run, stash=stash))

        elif command == "review-report":
            sys.exit(_cmd_review_report())

        elif command == "upgrade-check":
            sys.exit(
                _cmd_upgrade_check(
                    write_csv=getattr(args, "csv", False),
                    min_gap=getattr(args, "min_gap", 0),
                )
            )

        elif command == "integrity":
            stash = {}
            mf = getattr(args, "max_files", None)
            if mf is not None:
                stash["integrity_max_files"] = int(mf)
            sys.exit(_run_pipeline([IntegrityStage], dry_run=dry_run, stash=stash))

        elif command == "albumart":
            stash = {}
            if getattr(args, "force", False):
                stash["albumart_force"] = True
            if getattr(args, "no_embed", False):
                stash["albumart_embed"] = False
            sys.exit(_run_pipeline([AlbumArtStage], dry_run=dry_run, stash=stash))

        elif command == "overnight":
            overnight_pipeline = [
                GhostStage,
                HealthStage,
                NormalizeStage,
                EnrichStage,
                MBEnrichStage,
                NearDupeStage,
                ReviewerStage,
            ]
            sys.exit(_run_pipeline(overnight_pipeline, dry_run=dry_run))

        elif command == "playlist":
            sys.exit(_run_pipeline([PlaylistStage], dry_run=dry_run))

        elif command == "db-tune":
            sys.exit(_cmd_db_tune())

        elif command == "spec-scout":
            sys.exit(
                _cmd_spec_scout(
                    write_csv=getattr(args, "csv", False),
                    min_bitrate=getattr(args, "min_bitrate", 0),
                    max_bitrate=getattr(args, "max_bitrate", 0),
                )
            )

        elif command == "canon-review":
            canon_mode = getattr(args, "canon_mode", None)
            if not canon_mode:
                print("Usage: musaeus canon-review {report|apply}", file=sys.stderr)
                sys.exit(1)
            sys.exit(
                _cmd_canon_review(
                    mode=canon_mode,
                    csv_path=getattr(args, "csv", None),
                    fixes_path=getattr(args, "fixes", None),
                    dry_run=getattr(args, "dry_run", False),
                )
            )

        # ── review commands ───────────────────────────────────────────────────

        elif command == "dedupe":
            sys.exit(
                _cmd_dedupe(
                    auto=getattr(args, "auto", False),
                    report_only=getattr(args, "report", False),
                )
            )

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
