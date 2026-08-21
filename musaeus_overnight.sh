#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# MUSAEUS — Overnight Pipeline Runner (v3)
#
# Default: Ghost → `musaeus run` (full Act 1/2/3 + Enrichment canonical
#          chain, 21 stages, single process/run_id) → [Curator if requested]
#
# v3 replaces v2's hand-listed stage-by-stage calls (ingest/sentinel/scholar/
# forge/tagger, then separately health/enrich/neardupe) with one
# `musaeus run` call. v2 predated the Act 1/2/3 rebuild and had drifted out
# of sync with it: it never called Corrupt/Normalize/Sanitize/
# ArtistConsolidate/CrossDupe/DupeResolver/Canonicalize/Finalize/Audit at
# all, and separately called Health + NearDupe again even though the
# canonical chain now includes both. Calling `musaeus run` once keeps this
# script from having to be hand-updated every time a stage is added to the
# canonical chain in musaeus/stages/__init__.py.
#
# REMOVED 2026-08-18 (Grey's explicit call): the separate `run_stage enrich`
# / `run_stage mb-enrich` calls this script used to make after `run_stage
# run`. Both stages joined DEFAULT_PIPELINE as default-on on 2026-08-17
# (see musaeus/stages/__init__.py), so calling them again here duplicated
# work every night -- harmless (both are idempotent, already-enriched rows
# are skipped instantly) but wasteful, and exactly the kind of
# hand-listing-drifts-out-of-sync problem v3 already exists to avoid (see
# the paragraph above). `musaeus run` alone now covers them.
#
# NOTE on --minimal: previously meant "skip health/enrich/neardupe, keep
# core intake stages". Since Health + NearDupe + Enrich + MBEnrich are all
# now inside the single canonical `run` call (not separable without
# reintroducing per-stage hand-listing), --minimal means "run the
# canonical chain only, skip the remaining on-demand extras (Ghost/
# Curator)". Not used by the crontab entry below (no flags = full mode),
# so this is a forward-looking interface change, not a change to the
# scheduled behaviour.
#
# Usage:
#   ./musaeus_overnight.sh              # full pipeline (default)
#   ./musaeus_overnight.sh --minimal    # canonical run only (no ghost/curator)
#   ./musaeus_overnight.sh --with-curator # full + curator export
#   ./musaeus_overnight.sh --dry-run    # preview every stage, no mutations
#
# Schedule (cron example — run at 11pm):
#   0 23 * * * /mnt/FORGE2TB/Projects/MUSAEUS/musaeus_overnight.sh >> /mnt/FORGE2TB/Projects/MUSAEUS_VAULT/RUNS/overnight.log 2>&1
#
# Note: uses `python3 -m musaeus`, not the bare `musaeus` console script --
# cron's default PATH does not include ~/.local/bin, so the bare command
# fails with "command not found" under cron even though it works fine
# from an interactive shell. `python3 -m musaeus` only depends on the
# package being importable, which the editable pip install guarantees.
#
# Error recovery: each top-level stage below (ghost/run/curator)
# runs independently. A failure does NOT abort the script — subsequent
# stages still execute. Within `musaeus run` itself, cli.py's own
# resume-state mechanism handles resuming an interrupted canonical run on
# the next invocation.
#
# Disk guard: refuses to run at all if < MIN_FREE_GB free on vault filesystem
# (the canonical chain includes Forge + Canonicalize, both disk-sensitive).
# Refuses to run Curator if < CURATOR_MIN_FREE_GB free.
# ═══════════════════════════════════════════════════════════════════════════════

# Don't use set -e — we want stages to continue even if one fails.
set -uo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT_ROOT="${MUSAEUS_VAULT_ROOT:-/mnt/FORGE2TB/Projects/MUSAEUS_VAULT}"
LOG_DIR="${VAULT_ROOT}/RUNS/LOGS"
MIN_FREE_GB=50          # minimum free GB required to run the canonical chain
CURATOR_MIN_FREE_GB=400 # minimum free GB required to run Curator

# ── Parse flags ───────────────────────────────────────────────────────────────
MINIMAL=0
WITH_CURATOR=0
DRY_RUN=0

for arg in "$@"; do
    case "$arg" in
        --minimal)       MINIMAL=1 ;;
        --with-curator)  WITH_CURATOR=1 ;;
        --dry-run)       DRY_RUN=1 ;;
    esac
done

DRY_FLAG=""
if [[ "${DRY_RUN}" -eq 1 ]]; then
    DRY_FLAG="--dry-run"
fi

# ── Logging setup ─────────────────────────────────────────────────────────────
mkdir -p "${LOG_DIR}"
TIMESTAMP=$(date +"%Y%m%dT%H%M%S")
LOG_FILE="${LOG_DIR}/overnight_${TIMESTAMP}.log"

# Tee all output to log file
exec > >(tee -a "${LOG_FILE}") 2>&1

echo "═══════════════════════════════════════════════════════════════"
echo "  MUSAEUS Overnight Pipeline v3 — $(date '+%Y-%m-%d %H:%M:%S')"
echo "  Mode: $(if [[ "${MINIMAL}" -eq 1 ]]; then echo 'MINIMAL'; else echo 'FULL'; fi)$(if [[ "${DRY_RUN}" -eq 1 ]]; then echo ' [DRY RUN]'; fi)"
echo "  Log: ${LOG_FILE}"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# ── Failure alerting ──────────────────────────────────────────────────────────
# Nothing consumed this script's own exit code before now (no MAILTO in
# crontab, no local MTA installed anyway, and stdout/stderr are fully
# redirected into LOG_FILE above so cron's built-in mail-on-output never
# had anything to see even if MAILTO had been set). That's exactly how
# 4 consecutive nights of total failure (2026-08-04 through 2026-08-07,
# "musaeus: command not found" -- the cron-PATH bug the comment near the
# top of this file documents) went completely unnoticed until someone
# happened to read a log file days later. This sends a real push
# notification via ntfy.sh (scripts/musaeus_notify.py, a MUSAEUS-owned
# copy -- deliberately not an import from ORPHEUS's own notify.py, since
# depending on another project's script path is exactly the kind of
# fragile cross-project assumption that caused the original bug) on
# either an early abort (disk guard) or any stage failure. Deliberately
# NOT called for the concurrency-guard skip above -- that's an expected,
# working-as-designed outcome, not a failure, and alerting on it would
# just train it to be ignored.
notify_failure() {
    local subject="$1"
    local body="$2"
    if ! python3 "${SCRIPT_DIR}/scripts/musaeus_notify.py" \
        --title "MUSAEUS Overnight — ${subject}" \
        --message "${body}" \
        --tags "warning"; then
        echo "  ⚠ Failure notification could not be sent (see [notify] lines above)."
    fi
}

# ── Disk guard ────────────────────────────────────────────────────────────────
free_gb() {
    df -BG "${VAULT_ROOT}" 2>/dev/null | awk 'NR==2 {gsub("G",""); print $4}' || echo 0
}

FREE=$(free_gb)
echo "  Disk free: ${FREE} GB on ${VAULT_ROOT}"

if [[ "${FREE}" -lt "${MIN_FREE_GB}" ]]; then
    echo "  ✗ DISK GUARD: Only ${FREE} GB free — need ${MIN_FREE_GB} GB minimum."
    echo "    Aborting overnight run. Free up space and try again."
    notify_failure "Disk guard aborted the run" \
        "Only ${FREE} GB free on ${VAULT_ROOT} (need ${MIN_FREE_GB} GB minimum). No stage ran tonight. Time: $(date '+%Y-%m-%d %H:%M')"
    exit 1
fi

if [[ "${WITH_CURATOR}" -eq 1 && "${FREE}" -lt "${CURATOR_MIN_FREE_GB}" ]]; then
    echo "  ⚠ DISK GUARD: Curator needs ${CURATOR_MIN_FREE_GB} GB free, only ${FREE} GB available."
    echo "    Skipping curator — all other stages will run."
    WITH_CURATOR=0
fi

echo ""

# ── Concurrency guard ─────────────────────────────────────────────────────────
# Catches both invocation styles: the installed console-script entry point
# (bin/musaeus, e.g. an interactive session left running) and direct module
# invocation (python3 -m musaeus ..., what this script's own run_stage() uses
# for each stage). Checked once here, before any stage is started, so this
# script's own eventual subprocesses can't self-match. Confirmed empirically
# (pgrep -af against a real running musaeus console process and a synthetic
# "python3 -m musaeus run" process) that both branches match and pgrep does
# not self-match when run from a real script file. Without this guard, a
# collision here throws sqlite3.OperationalError: database is locked after
# the 10s busy_timeout in db.py expires (observed 2026-08-10 23:00: 0 passed,
# 4 failed, all four top-level stages hit this).
EXISTING=$(pgrep -af "(bin/musaeus\b|python3 -m musaeus\b)" || true)
if [[ -n "${EXISTING}" ]]; then
    echo "OVERNIGHT_SKIPPED_LOCK_COLLISION $(date '+%Y-%m-%dT%H:%M:%S') -- another musaeus process is already running, refusing to start (would hit 'database is locked'):"
    echo "${EXISTING}" | sed 's/^/    /'
    exit 0
fi

echo ""

# ── Counters ──────────────────────────────────────────────────────────────────
PASSED=0
FAILED=0
FAILED_STAGES=""

# ── Helper ────────────────────────────────────────────────────────────────────
# $1 = musaeus subcommand (e.g. "run", "ghost", "curator"). $DRY_FLAG is
# appended automatically when --dry-run was passed to this script, since
# every stage below (ghost/run/curator) supports it.
run_stage() {
    local stage="$1"
    echo "──────────────────────────────────────────────────────────────"
    echo "  Stage: ${stage}  — $(date '+%H:%M:%S')"
    echo "──────────────────────────────────────────────────────────────"
    if python3 -m musaeus "${stage}" ${DRY_FLAG} 2>&1; then
        echo "  ✓ ${stage} complete — $(date '+%H:%M:%S')"
        PASSED=$((PASSED + 1))
    else
        echo "  ✗ ${stage} FAILED — $(date '+%H:%M:%S')"
        FAILED=$((FAILED + 1))
        FAILED_STAGES="${FAILED_STAGES} ${stage}"
        # Non-fatal: continue to next stage
    fi
    echo ""
}

# ── Pipeline ──────────────────────────────────────────────────────────────────

# Phase 0: Ghost sweep FIRST (catches files moved/deleted since last run) --
# on-demand stage, not part of the canonical chain, skipped with --minimal.
if [[ "${MINIMAL}" -eq 0 ]]; then
    run_stage ghost
fi

# Phase 1: Canonical Act 1/2/3 + Enrichment chain (always runs) -- Preflight
# → Ingest → Permissions → Sentinel → Scholar → Health → Corrupt →
# AlbumArt → Normalize → Sanitize → ArtistConsolidate → CrossDupe →
# NearDupe → DupeResolver → Canonicalize → Finalize → Forge → Tagger →
# Audit → Enrich → MBEnrich, as one `musaeus run` process. See
# musaeus/stages/__init__.py CANONICAL_PIPELINE for the authoritative order
# and the reasoning behind it. Enrich/MBEnrich joined this chain
# 2026-08-17 (previously separate steps below -- see the REMOVED note at
# the top of this file). Canonicalize briefly ran ahead of dedup
# 2026-08-17, reverted 2026-08-18 -- see __init__.py's module docstring.
run_stage run

# Phase 2: Export (only when explicitly requested + disk space available)
if [[ "${WITH_CURATOR}" -eq 1 ]]; then
    FREE=$(free_gb)
    if [[ "${FREE}" -lt "${CURATOR_MIN_FREE_GB}" ]]; then
        echo "  ⚠ Curator skipped — only ${FREE} GB free (need ${CURATOR_MIN_FREE_GB} GB)"
    else
        run_stage curator
    fi
fi

# ── Final status ──────────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════"
echo "  Overnight pipeline complete — $(date '+%Y-%m-%d %H:%M:%S')"
echo "  Passed: ${PASSED}  |  Failed: ${FAILED}"
if [[ -n "${FAILED_STAGES}" ]]; then
    echo "  Failed stages:${FAILED_STAGES}"
fi
echo ""
python3 -m musaeus status 2>&1
echo "═══════════════════════════════════════════════════════════════"

# Exit with non-zero if any stage failed (useful for cron monitoring)
if [[ "${FAILED}" -gt 0 ]]; then
    notify_failure "${FAILED} stage(s) failed" \
        "Passed: ${PASSED}  Failed: ${FAILED}${FAILED_STAGES:+ (${FAILED_STAGES# })}. Log: ${LOG_FILE}. Time: $(date '+%Y-%m-%d %H:%M')"
    exit 1
fi
