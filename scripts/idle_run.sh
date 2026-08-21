#!/bin/bash
# MUSAEUS Idle-Aware Pipeline Run
#
# Sibling to scripts/idle_forge.sh, same reviewed pattern (2026-08-11
# session fixes: real X11 user-idle detection via musaeus_idle_ms.py,
# tracked-pidfile-only pause/resume), but targets a full `musaeus run`
# invocation instead of a standalone `musaeus forge` pass. Built
# 2026-08-19 for the first full USB2 backlog run, once BPM's Essentia
# analysis (genuinely CPU-heavy, confirmed against real files that same
# night) was wired into DEFAULT_PIPELINE and made a screensaver-style
# pause worth having for `musaeus run` itself, not just forge alone.
#
# idle_forge.sh's own comment deliberately excludes `musaeus run` from
# its scope ("never valid to SIGSTOP/SIGCONT just the forge part of a
# musaeus run process") -- that's about not conflating the two, not
# about pausing the whole `musaeus run` process being wrong. Pausing
# the *entire* pipeline while you're at the keyboard is exactly what's
# wanted here, so this is a separate script rather than a scope-creep
# change to idle_forge.sh.
#
# SIGSTOP nuance (same tradeoff idle_forge.sh already accepts for
# Forge's own ffmpeg subprocess calls, not a new one introduced here):
# SIGSTOP targets the tracked PID only, not a process group. BPM's
# Essentia analysis runs in-process (no subprocess), so it freezes
# immediately -- and BPM is the dominant CPU cost of a full run. An
# in-flight ffmpeg/ffprobe subprocess (Forge, Canonicalize, Scholar)
# finishes its current call before the next check can actually pause
# it, since SIGSTOP on the parent doesn't touch an already-spawned
# child -- a small, bounded delay, not an indefinite one.
#
# Completion detection differs from idle_forge.sh's: Forge has one
# meaningful cross-restart progress metric (rg_tagged_at count vs.
# CATALOGUED total); a full `musaeus run` touches ~20 different stage
# columns with no single analogous percentage. Completion here is
# tracked in-process (a shell variable set once the run actually
# starts) rather than re-derived from the DB on every check, so unlike
# idle_forge.sh it won't correctly detect "already complete" if this
# monitor itself is killed and restarted mid-run -- acceptable for a
# single foreground-launched session, not meant for the systemd-service
# always-on use idle_forge.sh is designed for.
#
# Usage:
#   ./scripts/idle_run.sh
#   IDLE_SECONDS_THRESHOLD=600 ./scripts/idle_run.sh
#
# Configuration via environment variables:
#   IDLE_SECONDS_THRESHOLD  - seconds of real user-idle time required
#                             before (re)starting/resuming the run (default: 300)
#   CHECK_INTERVAL          - seconds between checks (default: 60)
#
# Logs: /tmp/musaeus_idle_run.log

set -uo pipefail

# ── Configuration (can be overridden via environment) ──────────────────────────
IDLE_SECONDS_THRESHOLD=${IDLE_SECONDS_THRESHOLD:-300}  # 5 min real user-idle
CHECK_INTERVAL=${CHECK_INTERVAL:-60}
LOG_FILE="${LOG_FILE:-/tmp/musaeus_idle_run.log}"

RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp}"
LOCK_FILE="${RUNTIME_DIR}/musaeus_idle_run.lock"
CHILD_PID_FILE="${RUNTIME_DIR}/musaeus_idle_run_child.pid"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MUSAEUS_ROOT="$(dirname "$SCRIPT_DIR")"
IDLE_MS_HELPER="${SCRIPT_DIR}/musaeus_idle_ms.py"

cd "$MUSAEUS_ROOT" || exit 1

# ── Single-instance guard ───────────────────────────────────────────────────────
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
other_musaeus_running() {
    pgrep -af "(bin/musaeus\b|python3 -m musaeus\b)"
}

# ── Run child process management (tracked PID only) ─────────────────────────────

get_tracked_pid() {
    [ -f "$CHILD_PID_FILE" ] || return 1
    local pid
    pid=$(cat "$CHILD_PID_FILE" 2>/dev/null)
    [ -n "$pid" ] || return 1
    local cmd
    cmd=$(ps -o cmd= -p "$pid" 2>/dev/null)
    if [[ "$cmd" == *"musaeus"*"run"* ]]; then
        echo "$pid"
        return 0
    fi
    rm -f "$CHILD_PID_FILE"
    return 1
}

is_run_paused() {
    local pid=$1
    local state
    state=$(ps -o state= -p "$pid" 2>/dev/null | tr -d ' ')
    [ "$state" = "T" ]
}

pause_run() {
    local pid
    pid=$(get_tracked_pid) || return 1

    if ! is_run_paused "$pid"; then
        kill -STOP "$pid" 2>/dev/null && \
            log "⏸️  PAUSED musaeus run (PID: $pid) — you're active ($(idle_seconds_display))"
    fi
}

resume_run() {
    local pid
    if pid=$(get_tracked_pid); then
        if is_run_paused "$pid"; then
            kill -CONT "$pid" 2>/dev/null && \
                log "▶️  RESUMED musaeus run (PID: $pid) — $(idle_seconds_display)"
        fi
        return
    fi

    if [ "$RUN_STARTED" = "1" ]; then
        # Already started once and the tracked PID is gone -- it exited,
        # don't start a second one.
        return 1
    fi

    local others
    others=$(other_musaeus_running)
    if [ -n "$others" ]; then
        log "⏭️  Not starting run — another musaeus process is already running:"
        while IFS= read -r line; do
            log "      $line"
        done <<< "$others"
        return 1
    fi

    log "🚀 Starting musaeus run — $(idle_seconds_display)"
    nohup python3 -m musaeus run >> "$LOG_FILE" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$CHILD_PID_FILE"
    RUN_STARTED=1
    sleep 2
}

# ── Main loop ─────────────────────────────────────────────────────────────────

main() {
    log "=========================================="
    log "MUSAEUS Idle-Aware Pipeline Run Monitor"
    log "Idle threshold: ${IDLE_SECONDS_THRESHOLD}s of real user-idle time"
    log "Check interval: ${CHECK_INTERVAL}s"
    log "Lock file: $LOCK_FILE"
    log "Child PID file: $CHILD_PID_FILE"
    log "Log file: $LOG_FILE"
    log "=========================================="

    local last_state="unknown"
    local check_count=0
    RUN_STARTED=0

    while true; do
        check_count=$((check_count + 1))

        if [ "$RUN_STARTED" = "1" ] && ! get_tracked_pid >/dev/null; then
            log "✅ musaeus run finished (process exited). Check ${LOG_FILE} for the final Pipeline complete/errors line."
            break
        fi

        if is_system_idle; then
            if [ "$last_state" != "idle" ]; then
                resume_run
                last_state="idle"
            fi
        else
            if [ "$last_state" != "active" ]; then
                pause_run
                last_state="active"
            fi
        fi

        if [ $((check_count % 10)) -eq 0 ]; then
            log "📊 Status check (state: $last_state, $(idle_seconds_display))"
        fi

        sleep "$CHECK_INTERVAL"
    done

    log "Monitor exiting — run complete!"
}

# ── Entry point ───────────────────────────────────────────────────────────────

trap 'log "Interrupted — resuming run before exit..."; resume_run; exit 130' INT TERM

main "$@"
