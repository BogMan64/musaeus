#!/bin/bash
# MUSAEUS Idle-Aware LUFS Forge
# 
# Purpose: Automatically pause/resume LUFS forge processing based on system activity
# 
# This script monitors system CPU idle percentage and pauses the forge process when
# the system is busy, resuming it when idle. Perfect for long-running LUFS processing
# that you want to run in the background without impacting your work.
#
# Usage:
#   ./scripts/idle_forge.sh                    # Use default settings
#   IDLE_THRESHOLD=90 ./scripts/idle_forge.sh  # Custom idle threshold
#
# Configuration via environment variables:
#   IDLE_CPU_THRESHOLD  - CPU idle % threshold to start forge (default: 80)
#   CHECK_INTERVAL      - Seconds between checks (default: 60)
#
# The script will:
#   - Start forge when system is idle (CPU > 80% idle)
#   - Pause forge when system becomes active
#   - Resume forge when system becomes idle again
#   - Exit when all tracks are processed
#
# Logs: /tmp/musaeus_idle_forge.log

set -euo pipefail

# Configuration (can be overridden via environment)
IDLE_CPU_THRESHOLD=${IDLE_CPU_THRESHOLD:-80}  # Start if CPU idle > 80%
CHECK_INTERVAL=${CHECK_INTERVAL:-60}           # Check every 60 seconds
LOG_FILE="${LOG_FILE:-/tmp/musaeus_idle_forge.log}"

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MUSAEUS_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$MUSAEUS_ROOT"

# ── Logging ───────────────────────────────────────────────────────────────────

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# ── System monitoring ─────────────────────────────────────────────────────────

get_cpu_idle() {
    # Get idle CPU percentage from top
    # Use two samples to get accurate reading
    top -bn2 -d 0.5 | grep "Cpu(s)" | tail -1 | awk '{print $8}' | cut -d'%' -f1
}

is_system_idle() {
    local cpu_idle=$(get_cpu_idle)
    
    if [ -z "$cpu_idle" ]; then
        return 1  # Can't determine, assume busy
    fi
    
    # Check if idle percentage is above threshold
    local result=$(echo "$cpu_idle > $IDLE_CPU_THRESHOLD" | bc -l 2>/dev/null)
    [ "$result" = "1" ]
}

# ── Forge process management ──────────────────────────────────────────────────

get_forge_pid() {
    pgrep -f "musaeus forge" | head -1
}

is_forge_paused() {
    local pid=$1
    local state=$(ps -o state= -p "$pid" 2>/dev/null | tr -d ' ')
    [ "$state" = "T" ]
}

pause_forge() {
    local pid=$(get_forge_pid)
    
    if [ -z "$pid" ]; then
        return 1
    fi
    
    if ! is_forge_paused "$pid"; then
        kill -STOP "$pid" 2>/dev/null && \
            log "⏸️  PAUSED forge (PID: $pid) - System active (CPU idle: $(get_cpu_idle)%)"
    fi
}

resume_forge() {
    local pid=$(get_forge_pid)
    
    if [ -n "$pid" ]; then
        if is_forge_paused "$pid"; then
            kill -CONT "$pid" 2>/dev/null && \
                log "▶️  RESUMED forge (PID: $pid) - System idle (CPU idle: $(get_cpu_idle)%)"
        fi
    else
        # No forge running, start it
        log "🚀 Starting forge - System idle (CPU idle: $(get_cpu_idle)%)"
        nohup python3 -m musaeus forge >> "$LOG_FILE" 2>&1 &
        sleep 2
    fi
}

# ── Progress tracking ─────────────────────────────────────────────────────────

get_progress() {
    python3 -c "
from musaeus.config import get_config
from musaeus.db import open_db
try:
    cfg = get_config()
    conn = open_db(cfg.db_path)
    forged = conn.execute(
        'SELECT COUNT(*) FROM archive WHERE rg_tagged_at IS NOT NULL'
    ).fetchone()[0]
    total = conn.execute(
        \"SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'\"
    ).fetchone()[0]
    conn.close()
    pct = 100.0 * forged / total if total > 0 else 0
    print(f'{forged:,}/{total:,} ({pct:.1f}%)')
except Exception as e:
    print(f'Error: {e}')
" 2>/dev/null
}

is_forge_complete() {
    local progress=$(get_progress)
    [[ "$progress" == *"100.0%"* ]]
}

# ── Main loop ─────────────────────────────────────────────────────────────────

main() {
    log "=========================================="
    log "MUSAEUS Idle-Aware Forge Monitor"
    log "CPU idle threshold: ${IDLE_CPU_THRESHOLD}%"
    log "Check interval: ${CHECK_INTERVAL}s"
    log "Log file: $LOG_FILE"
    log "=========================================="
    
    local last_state="unknown"
    local check_count=0
    
    while true; do
        check_count=$((check_count + 1))
        
        # Check if forge is complete
        if ! pgrep -f "musaeus forge" > /dev/null; then
            if is_forge_complete; then
                local final_progress=$(get_progress)
                log "✅ Forge complete! Final: $final_progress"
                break
            fi
        fi
        
        # Check system state and act accordingly
        if is_system_idle; then
            if [ "$last_state" != "idle" ]; then
                resume_forge
                last_state="idle"
            fi
        else
            if [ "$last_state" != "active" ]; then
                pause_forge
                local progress=$(get_progress)
                log "📊 Progress: $progress"
                last_state="active"
            fi
        fi
        
        # Periodic progress update (every 10 checks)
        if [ $((check_count % 10)) -eq 0 ]; then
            local progress=$(get_progress)
            log "📊 Status check: $progress (state: $last_state, CPU idle: $(get_cpu_idle)%)"
        fi
        
        sleep "$CHECK_INTERVAL"
    done
    
    log "Monitor exiting - forge complete!"
}

# ── Entry point ───────────────────────────────────────────────────────────────

# Trap Ctrl+C to gracefully resume forge before exiting
trap 'log "Interrupted - resuming forge before exit..."; resume_forge; exit 130' INT TERM

main "$@"
