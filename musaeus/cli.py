#!/usr/bin/env python3
"""
MUSAEUS — CLI entry point.

Usage:
    musaeus [command] [options]

Pipeline commands:
    run              Run the default pipeline (Ingest → Sentinel → Scholar)
    run --full       Run the full pipeline  (+ Normalize → Forge → Tagger)
    run --maintain   Run the maintenance pipeline (Ghost→Health→Normalize→Enrich→MBEnrich→NearDupe)
    run --enrich     Run the enrichment pipeline (Enrich→MBEnrich→AcousticID→Reviewer)
    dry-run          Preview the default pipeline without any mutations
    ingest           Run Ingest stage only
    sentinel         Run Sentinel stage only
    scholar          Run Scholar stage only
    normalize        Article-suffix fix + ALL-CAPS repair on archived metadata
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
    MAINTAIN_PIPELINE,
    AcousticIDStage,
    AuditorStage,
    CuratorStage,
    EnrichStage,
    ForgeStage,
    GhostStage,
    HealthStage,
    IngestStage,
    MBEnrichStage,
    NearDupeStage,
    NormalizeStage,
    ReviewerStage,
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

    # ── run ──────────────────────────────────────────────────────────────────
    run_p = sub.add_parser("run", help="Run pipeline (Ingest→Sentinel→Scholar)")
    run_p.add_argument("--dry-run", action="store_true", help="Preview only, no mutations")
    run_p.add_argument("--full", action="store_true", help="Also run Forge + Tagger stages")
    run_p.add_argument(
        "--maintain", action="store_true", help="Run Ghost + Health + Enrich + NearDupe"
    )
    run_p.add_argument(
        "--enrich",
        action="store_true",
        help="Run Enrich + MBEnrich + AcousticID + Reviewer pipeline",
    )

    # dry-run shortcut
    sub.add_parser("dry-run", help="Alias for: run --dry-run")

    # ── individual stages ─────────────────────────────────────────────────────
    for name in ("ingest", "sentinel", "scholar"):
        sp = sub.add_parser(name, help=f"Run {name} stage only")
        sp.add_argument("--dry-run", action="store_true", help="Preview only")

    # normalize
    normalize_p = sub.add_parser("normalize", help="Article-suffix fix + ALL-CAPS repair")
    normalize_p.add_argument("--dry-run", action="store_true", help="Preview only, no DB changes")

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

    # ── review commands ───────────────────────────────────────────────────────

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

    _setup_logging(getattr(args, "verbose", False))

    command = args.command or "console"
    dry_run = getattr(args, "dry_run", False)

    try:
        # ── pipeline commands ─────────────────────────────────────────────────

        if command == "run":
            if getattr(args, "maintain", False):
                pipeline = MAINTAIN_PIPELINE
            elif getattr(args, "full", False):
                pipeline = FULL_PIPELINE
            elif getattr(args, "enrich", False):
                pipeline = ENRICH_PIPELINE
            else:
                pipeline = DEFAULT_PIPELINE
            sys.exit(_run_pipeline(pipeline, dry_run=dry_run))

        elif command == "dry-run":
            sys.exit(_run_pipeline(DEFAULT_PIPELINE, dry_run=True))

        elif command == "ingest":
            sys.exit(_run_pipeline([IngestStage], dry_run=dry_run))

        elif command == "sentinel":
            sys.exit(_run_pipeline([SentinelStage], dry_run=dry_run))

        elif command == "scholar":
            sys.exit(_run_pipeline([ScholarStage], dry_run=dry_run))

        elif command == "normalize":
            sys.exit(_run_pipeline([NormalizeStage], dry_run=dry_run))

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
