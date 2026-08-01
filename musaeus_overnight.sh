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
    if musaeus "${stage}" 2>&1; then
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

# Phase 2b: Auto-resolve exact duplicates (always runs unless minimal)
if [[ "${MINIMAL}" -eq 0 ]]; then
    echo "──────────────────────────────────────────────────────────────"
    echo "  Stage: resolve-exact-dupes  — $(date '+%H:%M:%S')"
    echo "──────────────────────────────────────────────────────────────"
    if python3 scripts/resolve_exact_dupes.py --apply 2>&1; then
        echo "  ✓ resolve-exact-dupes complete — $(date '+%H:%M:%S')"
        PASSED=$((PASSED + 1))
    else
        echo "  ✗ resolve-exact-dupes FAILED — $(date '+%H:%M:%S')"
        FAILED=$((FAILED + 1))
        FAILED_STAGES="${FAILED_STAGES} resolve-exact-dupes"
    fi
    echo ""
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

# ── Health assertions ─────────────────────────────────────────────────────────
echo "  Log file: ${LOG_FILE}"
echo ""
echo ""
musaeus status 2>&1
echo ""

# Check for unresolved debt
PENDING_DUPES=$(python3 -c "
import sqlite3, os, sys
sys.path.insert(0, '.')
try:
    from musaeus.config import get_config
    db = get_config().db_path
except Exception:
    db = '${VAULT_ROOT}/musaeus.db'
conn = sqlite3.connect(str(db))
count = conn.execute(\"SELECT COUNT(*) FROM duplicates WHERE status = 'pending'\").fetchone()[0]
print(count)
conn.close()
" 2>/dev/null || echo "?")

JUNK_COUNT=$(python3 -c "
import sqlite3, os, sys
sys.path.insert(0, '.')
from musaeus.stages.content_filter import is_junk_by_fields
try:
    from musaeus.config import get_config
    db = get_config().db_path
except Exception:
    db = '${VAULT_ROOT}/musaeus.db'
conn = sqlite3.connect(str(db))
conn.row_factory = sqlite3.Row
rows = conn.execute(\"SELECT artist, title FROM archive WHERE status NOT IN ('QUARANTINE', 'DUPE')\").fetchall()
junk = sum(1 for r in rows if is_junk_by_fields(r['artist'] or '', r['title'] or ''))
print(junk)
conn.close()
" 2>/dev/null || echo "?")

echo "  ── Health Assertions ──────────────────────────────────────"
if [[ "${PENDING_DUPES}" != "?" && "${PENDING_DUPES}" -gt 0 ]]; then
    echo "  ⚠ WARNING: ${PENDING_DUPES} duplicate members still pending resolution"
else
    echo "  ✓ No pending duplicates"
fi

if [[ "${JUNK_COUNT}" != "?" && "${JUNK_COUNT}" -gt 0 ]]; then
    echo "  ⚠ WARNING: ${JUNK_COUNT} junk track(s) detected in archive (run content_filter --purge)"
else
    echo "  ✓ No junk content in archive"
fi

if [[ "${FAILED}" -gt 0 ]]; then
    echo "  ✗ PIPELINE HAD FAILURES — check log: ${LOG_FILE}"
else
    echo "  ✓ All stages passed"
fi
echo "═══════════════════════════════════════════════════════════════"

# Exit with non-zero if any stage failed (useful for cron monitoring)
if [[ "${FAILED}" -gt 0 ]]; then
    exit 1
fi
