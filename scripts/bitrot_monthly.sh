#!/usr/bin/env bash
#
# The bit-rot check, every month on its own (Grey, 2026-10-06: "every 30 days").
# Run by ~/.config/systemd/user/musaeus-bitrot.timer.
#
# Waits until no other MUSAEUS work is running (a repair writes masters, and a
# build or a run may be moving them), then runs `musaeus bitrot`, which repairs
# rot from the newest good backup on its own. A summary is sent EVERY time, also
# when all is well: as with the backups, no report is the alarm.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${MUSAEUS_BITROT_LOG_DIR:-$HOME/.local/state/musaeus}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/bitrot_$(date +%Y%m%d_%H%M).log"
MUSAEUS="${MUSAEUS_BIN:-$HOME/.local/bin/musaeus}"
notify() { python3 "$REPO/scripts/musaeus_notify.py" --title "MUSAEUS bit-rot check" --message "$1" >/dev/null 2>&1 || true; }

busy() { ps -eo args | awk '$1 ~ /python/ && $2 ~ /\/musaeus$/' | grep -q .; }
waited=0
while busy; do
    if [ "$waited" -ge 720 ]; then
        notify "not run: other MUSAEUS work was running for 12 hours; next try next month (or run: musaeus bitrot)"
        exit 0
    fi
    sleep 600; waited=$((waited + 10))
done

"$MUSAEUS" bitrot > "$LOG" 2>&1
rc=$?
summary="$(grep -E "^\s*(ok:|corrupt|REPAIRED|NOT repaired|replaced on purpose|missing from disk|NOT A CLEAN)" "$LOG" | sed 's/^\s*//' | head -6 | paste -sd ';' -)"
notify "${summary:-finished (exit $rc)} -- log: $LOG"
exit $rc
