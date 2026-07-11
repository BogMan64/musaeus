#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
# MUSAEUS — Overnight Pipeline Runner
# Runs: Ingest → Sentinel → Scholar → Forge
# Skips: Curator (copies files — only run when space is confirmed)
#
# Usage:
#   ./musaeus_overnight.sh              # default run
#   ./musaeus_overnight.sh --with-enrich  # also run enrich (Last.fm)
#   ./musaeus_overnight.sh --with-neardupe # also run neardupe
#   ./musaeus_overnight.sh --full        # all stages including curator
#
# Schedule (cron example — run at 11pm):
#   0 23 * * * /mnt/FORGE2TB/Projects/MUSAEUS/musaeus_overnight.sh >> /mnt/FORGE2TB/Projects/MUSAEUS_VAULT/RUNS/overnight.log 2>&1
#
# Disk guard: refuses to run Forge if < MIN_FREE_GB free on vault filesystem.
# Refuses to run Curator if < CURATOR_MIN_FREE_GB free.
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
VAULT_ROOT="${MUSAEUS_VAULT_ROOT:-/mnt/FORGE2TB/Projects/MUSAEUS_VAULT}"
LOG_DIR="${VAULT_ROOT}/RUNS/LOGS"
MIN_FREE_GB=50          # minimum free GB required to run Forge
CURATOR_MIN_FREE_GB=400 # minimum free GB required to run Curator

# ── Parse flags ───────────────────────────────────────────────────────────────
WITH_ENRICH=0
WITH_NEARDUPE=0
WITH_CURATOR=0

for arg in "$@"; do
    case "$arg" in
        --with-enrich)   WITH_ENRICH=1 ;;
        --with-neardupe) WITH_NEARDUPE=1 ;;
        --with-curator)  WITH_CURATOR=1 ;;
        --full)
            WITH_ENRICH=1
            WITH_NEARDUPE=1
            WITH_CURATOR=1
            ;;
    esac
done

# ── Logging setup ─────────────────────────────────────────────────────────────
mkdir -p "${LOG_DIR}"
TIMESTAMP=$(date +"%Y%m%dT%H%M%S")
LOG_FILE="${LOG_DIR}/overnight_${TIMESTAMP}.log"

# Tee all output to log file
exec > >(tee -a "${LOG_FILE}") 2>&1

echo "═══════════════════════════════════════════════════════════════"
echo "  MUSAEUS Overnight Pipeline — $(date '+%Y-%m-%d %H:%M:%S')"
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

# ── Helper ────────────────────────────────────────────────────────────────────
run_stage() {
    local stage="$1"
    echo "──────────────────────────────────────────────────────────────"
    echo "  Stage: ${stage}  — $(date '+%H:%M:%S')"
    echo "──────────────────────────────────────────────────────────────"
    if musaeus "${stage}" 2>&1; then
        echo "  ✓ ${stage} complete — $(date '+%H:%M:%S')"
    else
        echo "  ✗ ${stage} failed — $(date '+%H:%M:%S')"
        # Non-fatal: continue to next stage
    fi
    echo ""
}

# ── Pipeline ──────────────────────────────────────────────────────────────────

# Always run core pipeline
run_stage ingest
run_stage sentinel
run_stage scholar
run_stage forge

# Optional stages
if [[ "${WITH_ENRICH}" -eq 1 ]]; then
    run_stage enrich
fi

if [[ "${WITH_NEARDUPE}" -eq 1 ]]; then
    run_stage neardupe
fi

if [[ "${WITH_CURATOR}" -eq 1 ]]; then
    # Re-check disk before curator (it writes files)
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
echo ""
musaeus status 2>&1
echo "═══════════════════════════════════════════════════════════════"
