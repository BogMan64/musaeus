#!/bin/bash
# MUSAEUS Idle-Aware LUFS Forge
#
# Purpose: Automatically pause/resume a standalone `musaeus forge` LUFS
# backfill based on genuine user-idle time (like a screensaver), so a
# 30+ hour LUFS pass can run in the background without competing with
# you for CPU while you're actually at the keyboard.
#
# Scope note (2026-08-11 session): Forge is now ALSO stage 15 of 17
# inside the single unified `musaeus run` canonical pipeline
# (musaeus/stages/__init__.py CANONICAL_PIPELINE) as of the Act 1/2/3
# restructuring. This script only ever targets a STANDALONE
# `python3 -m musaeus forge` invocation that it itself launches -- it
# was never valid, and still isn't, to SIGSTOP/SIGCONT "just the forge
# part" of a `musaeus run` process; that would pause the whole 17-stage
# chain. If a `musaeus run` (or any other musaeus process) is already
# active, this script refuses to start a competing forge run at all
# (see other_musaeus_running() below) rather than risk a
# sqlite3 "database is locked" collision.
#
# Two bugs fixed from the original version (Grey, 2026-08-11 session):
#   1. Idle detection was CPU-idle (`top`), which is backwards -- CPU
#      sits near-idle during normal light desktop use, so the old check
#      would "resume" while you're actively at the keyboard. Replaced
#      with real X11 user-idle time (milliseconds since last keyboard/
#      mouse input, via the XScreenSaver extension -- the same
#      mechanism xprintidle uses), read through scripts/musaeus_idle_ms.py
#      using python3-xlib (already installed, no new package/sudo
#      needed). This is screensaver-like: IDLE_SECONDS_THRESHOLD is how
#      long you must be AWAY, not how quiet the CPU is.
#   2. PID tracking was `pgrep -f "musaeus forge"`, which matches ANY
#      process with that substring in its command line -- not
#      necessarily one this script launched. A manual
#      `python3 -m musaeus forge` run for testing, started by you while
#      this script happened to be active, would get seized and
#      SIGSTOP'd by mistake. Replaced with a dedicated pidfile tracking
#      only the exact child PID this script itself launched; pause/
#      resume never act on any other PID, and a cmdline sanity check
#      guards against a stale pidfile pointing at a since-recycled PID.
#
# Usage:
#   ./scripts/idle_forge.sh                       # default settings
#   IDLE_SECONDS_THRESHOLD=600 ./scripts/idle_forge.sh
#
# Configuration via environment variables:
#   IDLE_SECONDS_THRESHOLD  - seconds of real user-idle time required
#                             before (re)starting/resuming forge (default: 300)
#   CHECK_INTERVAL          - seconds between checks (default: 60)
#
# Concurrency: this script is meant to be started once and left running
# (see systemd unit at the bottom of this comment block). It takes an
# flock on its own lockfile at startup and exits immediately if another
# copy already holds it -- a second instance can never stack on top of
# an existing one.
#
# Recommended launch: systemd --user service, not cron (this is a
# persistent foreground loop, not a periodic job) and not bare nohup
# (no restart-on-crash, no `systemctl status`/journal integration).
# See scripts/musaeus-idle-forge.service for a ready-to-install unit,
# matching the same pattern as this box's existing
# ~/.config/systemd/user/orpheus-dashboard.service.
#
# Logs: /tmp/musaeus_idle_forge.log

set -uo pipefail

# ── Configuration (can be overridden via environment) ──────────────────────────
IDLE_SECONDS_THRESHOLD=${IDLE_SECONDS_THRESHOLD:-300}  # 5 min real user-idle
CHECK_INTERVAL=${CHECK_INTERVAL:-60}
LOG_FILE="${LOG_FILE:-/tmp/musaeus_idle_forge.log}"

RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp}"
LOCK_FILE="${RUNTIME_DIR}/musaeus_idle_forge.lock"
CHILD_PID_FILE="${RUNTIME_DIR}/musaeus_idle_forge_child.pid"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MUSAEUS_ROOT="$(dirname "$SCRIPT_DIR")"
IDLE_MS_HELPER="${SCRIPT_DIR}/musaeus_idle_ms.py"

cd "$MUSAEUS_ROOT" || exit 1

# ── Single-instance guard ───────────────────────────────────────────────────────
# FD-based flock, non-blocking: open the lockfile on FD 200 in THIS
# process (no re-exec) and try to take an exclusive lock on it. A second
# copy fails the flock immediately and exits instead of queuing up
# behind, or worse, running concurrently with, the first.
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Another instance already holds ${LOCK_FILE} — exiting." >&2
    exit 1
fi

# ── Logging ───────────────────────────────────────────────────────────────────

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# ── Idle detection (real user-idle time, not CPU-idle) ──────────────────────────

get_idle_ms() {
    python3 "$IDLE_MS_HELPER" 2>>"$LOG_FILE"
}

is_system_idle() {
    local idle_ms
    idle_ms=$(get_idle_ms)
    if [ -z "$idle_ms" ]; then
        return 1  # can't determine (no X session reachable) -- assume busy
    fi
    [ "$idle_ms" -ge $((IDLE_SECONDS_THRESHOLD * 1000)) ]
}

idle_seconds_display() {
    local idle_ms
    idle_ms=$(get_idle_ms)
    if [ -n "$idle_ms" ]; then
        echo "$((idle_ms / 1000))s idle"
    else
        echo "idle time unknown"
    fi
}

# ── Concurrency guard against OTHER musaeus processes ───────────────────────────
# Same pattern as musaeus_overnight.sh's concurrency guard: catches both
# invocation styles (installed console-script entry point and direct
# module invocation). Used only as a pre-start check -- if some other
# musaeus process (a `musaeus run`, a manual invocation, anything) is
# already active, this script refuses to start a NEW forge run rather
# than risk a "database is locked" collision. It is NOT used for pause/
# resume, which only ever acts on the pidfile-tracked child (see below).
other_musaeus_running() {
    pgrep -af "(bin/musaeus\b|python3 -m musaeus\b)"
}

# ── Forge child process management (tracked PID only) ───────────────────────────

get_tracked_pid() {
    [ -f "$CHILD_PID_FILE" ] || return 1
    local pid
    pid=$(cat "$CHILD_PID_FILE" 2>/dev/null)
    [ -n "$pid" ] || return 1
    # Sanity check: the PID must still exist AND still actually be our
    # forge child, not some unrelated process that reused the PID after
    # our child exited (a stale pidfile pointing at a recycled PID would
    # otherwise let us SIGSTOP a completely unrelated process).
    local cmd
    cmd=$(ps -o cmd= -p "$pid" 2>/dev/null)
    if [[ "$cmd" == *"musaeus"*"forge"* ]]; then
        echo "$pid"
        return 0
    fi
    rm -f "$CHILD_PID_FILE"
    return 1
}

is_forge_paused() {
    local pid=$1
    local state
    state=$(ps -o state= -p "$pid" 2>/dev/null | tr -d ' ')
    [ "$state" = "T" ]
}

pause_forge() {
    local pid
    pid=$(get_tracked_pid) || return 1

    if ! is_forge_paused "$pid"; then
        kill -STOP "$pid" 2>/dev/null && \
            log "⏸️  PAUSED forge (PID: $pid) — you're active ($(idle_seconds_display))"
    fi
}

resume_forge() {
    local pid
    if pid=$(get_tracked_pid); then
        if is_forge_paused "$pid"; then
            kill -CONT "$pid" 2>/dev/null && \
                log "▶️  RESUMED forge (PID: $pid) — $(idle_seconds_display)"
        fi
        return
    fi

    # No tracked child -- only start a fresh one if nothing else musaeus-
    # related is already running (avoids a DB-lock collision).
    local others
    others=$(other_musaeus_running)
    if [ -n "$others" ]; then
        log "⏭️  Not starting forge — another musaeus process is already running:"
        while IFS= read -r line; do
            log "      $line"
        done <<< "$others"
        return 1
    fi

    log "🚀 Starting forge — $(idle_seconds_display)"
    nohup python3 -m musaeus forge >> "$LOG_FILE" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$CHILD_PID_FILE"
    sleep 2
}

# ── Progress tracking ─────────────────────────────────────────────────────────
# Opens the real musaeus.db read-only-in-effect (SELECT COUNT only, no
# writes performed here) via the normal open_db() path -- same as every
# other musaeus command. Only invoked once this script is actually
# running for real; not touched by editing/reviewing this script.

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
    local progress
    progress=$(get_progress)
    [[ "$progress" == *"100.0%"* ]]
}

# ── Main loop ─────────────────────────────────────────────────────────────────

main() {
    log "=========================================="
    log "MUSAEUS Idle-Aware Forge Monitor"
    log "Idle threshold: ${IDLE_SECONDS_THRESHOLD}s of real user-idle time"
    log "Check interval: ${CHECK_INTERVAL}s"
    log "Lock file: $LOCK_FILE"
    log "Child PID file: $CHILD_PID_FILE"
    log "Log file: $LOG_FILE"
    log "=========================================="

    local last_state="unknown"
    local check_count=0

    while true; do
        check_count=$((check_count + 1))

        if ! get_tracked_pid >/dev/null; then
            if is_forge_complete; then
                local final_progress
                final_progress=$(get_progress)
                log "✅ Forge complete! Final: $final_progress"
                break
            fi
        fi

        if is_system_idle; then
            if [ "$last_state" != "idle" ]; then
                resume_forge
                last_state="idle"
            fi
        else
            if [ "$last_state" != "active" ]; then
                pause_forge
                local progress
                progress=$(get_progress)
                log "📊 Progress: $progress"
                last_state="active"
            fi
        fi

        if [ $((check_count % 10)) -eq 0 ]; then
            local progress
            progress=$(get_progress)
            log "📊 Status check: $progress (state: $last_state, $(idle_seconds_display))"
        fi

        sleep "$CHECK_INTERVAL"
    done

    log "Monitor exiting — forge complete!"
}

# ── Entry point ───────────────────────────────────────────────────────────────

trap 'log "Interrupted — resuming forge before exit..."; resume_forge; exit 130' INT TERM

main "$@"
