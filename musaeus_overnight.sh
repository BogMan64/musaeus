#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# MUSAEUS — Overnight Pipeline Runner (v2)
# Default: full pipeline (Ingest → Sentinel → Scholar → Forge → Tagger →
#          Ghost → Health → Enrich → NearDupe)
# Curator only runs when disk space is confirmed.
#
# Usage:
#   ./musaeus_overnight.sh              # full pipeline (default)
#   ./musaeus_overnight.sh --minimal    # core only (no enrich/neardupe/curator)
#   ./musaeus_overnight.sh --with-curator # full + curator export
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
# Error recovery: each stage runs independently. A failed stage does NOT
# abort the pipeline — subsequent stages still execute.
#
# Disk guard: refuses to run Forge if < MIN_FREE_GB free on vault filesystem.
# Refuses to run Curator if < CURATOR_MIN_FREE_GB free.
# ═══════════════════════════════════════════════════════════════════════════════

# Don't use set -e — we want stages to continue even if one fails.
set -uo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
VAULT_ROOT="${MUSAEUS_VAULT_ROOT:-/mnt/FORGE2TB/Projects/MUSAEUS_VAULT}"
LOG_DIR="${VAULT_ROOT}/RUNS/LOGS"
MIN_FREE_GB=50          # minimum free GB required to run Forge
CURATOR_MIN_FREE_GB=400 # minimum free GB required to run Curator

# ── Parse flags ───────────────────────────────────────────────────────────────
# Default: full pipeline (enrich + neardupe enabled)
MINIMAL=0
WITH_CURATOR=0

for arg in "$@"; do
    case "$arg" in
        --minimal)       MINIMAL=1 ;;
        --with-curator)  WITH_CURATOR=1 ;;
    esac
done

# ── Logging setup ─────────────────────────────────────────────────────────────
mkdir -p "${LOG_DIR}"
TIMESTAMP=$(date +"%Y%m%dT%H%M%S")
LOG_FILE="${LOG_DIR}/overnight_${TIMESTAMP}.log"

# Tee all output to log file
exec > >(tee -a "${LOG_FILE}") 2>&1

echo "═══════════════════════════════════════════════════════════════"
echo "  MUSAEUS Overnight Pipeline v2 — $(date '+%Y-%m-%d %H:%M:%S')"
echo "  Mode: $(if [[ "${MINIMAL}" -eq 1 ]]; then echo 'MINIMAL'; else echo 'FULL'; fi)"
echo "  Log: ${LOG_FILE}"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# ── Disk guard ────────────────────────────────────────────────────────────────
free_gb() {
    df -BG "${VAULT_ROOT}" 2>/dev/null | awk 'NR==2 {gsub("G",""); print $4}' || echo 0
}

FREE=$(free_gb)
echo "  Disk free: ${FREE} GB on ${VAULT_ROOT}"

if [[ "${FREE}" -lt "${MIN_FREE_GB}" ]]; then
    echo "  ✗ DISK GUARD: Only ${FREE} GB free — need ${MIN_FREE_GB} GB minimum."
    echo "    Aborting overnight run. Free up space and try again."
    exit 1
fi

if [[ "${WITH_CURATOR}" -eq 1 && "${FREE}" -lt "${CURATOR_MIN_FREE_GB}" ]]; then
    echo "  ⚠ DISK GUARD: Curator needs ${CURATOR_MIN_FREE_GB} GB free, only ${FREE} GB available."
    echo "    Skipping curator — all other stages will run."
    WITH_CURATOR=0
fi

echo ""

# ── Counters ──────────────────────────────────────────────────────────────────
PASSED=0
FAILED=0
FAILED_STAGES=""

# ── Helper ────────────────────────────────────────────────────────────────────
run_stage() {
    local stage="$1"
    echo "──────────────────────────────────────────────────────────────"
    echo "  Stage: ${stage}  — $(date '+%H:%M:%S')"
    echo "──────────────────────────────────────────────────────────────"
    if python3 -m musaeus "${stage}" 2>&1; then
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

# Phase 0: Ghost sweep FIRST (catches files moved/deleted since last run)
run_stage ghost

# Phase 1: Core pipeline (always runs)
run_stage ingest
run_stage sentinel
run_stage scholar
run_stage forge
run_stage tagger

# Phase 2: Maintenance (runs by default, skip with --minimal)
if [[ "${MINIMAL}" -eq 0 ]]; then
    run_stage health
    run_stage enrich
    run_stage neardupe
fi

# Phase 3: Export (only when explicitly requested + disk space available)
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
    exit 1
fi
