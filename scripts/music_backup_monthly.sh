#!/usr/bin/env bash
#
# The masters' backup to NUC8TB, every month on its own (Grey, 2026-10-07).
# Run by ~/.config/systemd/user/musaeus-music-backup.timer.
#
# Waits until no other MUSAEUS work is running (masters must not move during the
# copy), then runs scripts/music_backup.py --execute. A summary is sent EVERY
# time: as with the other backups, no report is the alarm.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${MUSAEUS_BACKUP_LOG_DIR:-$HOME/.local/state/musaeus}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/music_backup_$(date +%Y%m%d_%H%M).log"
notify() { python3 "$REPO/scripts/musaeus_notify.py" --title "MUSAEUS music backup" --message "$1" >/dev/null 2>&1 || true; }

# The backup holds the masters lock (shared) and exits 75 while a job that
# changes masters holds it: retry every 10 minutes, for up to 12 hours. The
# lock replaced a guess from the process list (review of #87, finding 10).
waited=0
while :; do
    python3 "$REPO/scripts/music_backup.py" --execute > "$LOG" 2>&1
    rc=$?
    [ "$rc" -ne 75 ] && break
    if [ "$waited" -ge 720 ]; then
        notify "not run: other MUSAEUS work held the masters for 12 hours; run by hand: python3 $REPO/scripts/music_backup.py --execute"
        exit 0
    fi
    sleep 600; waited=$((waited + 10))
done
notify "$(grep -E '^(BACKUP|NOT RUN)' "$LOG" | head -2 | paste -sd ' ' -) (log: $LOG)"
exit $rc
