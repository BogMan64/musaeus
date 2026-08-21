# MUSAEUS Utility Scripts

Helper scripts for MUSAEUS music library management.

---

## `idle_forge.sh`

**Idle-aware LUFS forge processor** - Automatically pauses/resumes forge based on system activity.

### Purpose

LUFS measurement is CPU-intensive and can take 30+ hours for large libraries. This script allows forge to run in the background, automatically pausing when you're using the computer and resuming when idle.

### Usage

```bash
# Basic usage (default: pause when CPU < 80% idle)
./scripts/idle_forge.sh

# Custom idle threshold (pause when CPU < 90% idle)
IDLE_CPU_THRESHOLD=90 ./scripts/idle_forge.sh

# Custom check interval (check every 30 seconds)
CHECK_INTERVAL=30 ./scripts/idle_forge.sh

# Run in background with nohup
nohup ./scripts/idle_forge.sh &

# Monitor progress
tail -f /tmp/musaeus_idle_forge.log
```

### Configuration

Environment variables:

- `IDLE_CPU_THRESHOLD` - CPU idle % threshold (default: 80)
  - System is considered "idle" when CPU idle > this value
  - Higher values = more conservative (pauses more often)
  - Lower values = more aggressive (runs more often)

- `CHECK_INTERVAL` - Seconds between checks (default: 60)
  - How often to check system state
  - Lower values = more responsive but more overhead
  - Higher values = less responsive but less overhead

- `LOG_FILE` - Log file path (default: `/tmp/musaeus_idle_forge.log`)

### How It Works

1. **Monitors CPU idle percentage** using `top`
2. **Starts forge** when system is idle (CPU > 80% idle)
3. **Pauses forge** (SIGSTOP) when system becomes active
4. **Resumes forge** (SIGCONT) when system becomes idle again
5. **Exits** when all tracks are processed (100% complete)

### Process States

- `▶️ RESUMED` - Forge is running
- `⏸️ PAUSED` - Forge is paused (waiting for idle)
- `🚀 Starting` - Forge process starting
- `✅ Complete` - All tracks processed

### Examples

#### Conservative (desktop work)
```bash
# Only run when CPU is very idle (>90%)
IDLE_CPU_THRESHOLD=90 CHECK_INTERVAL=30 ./scripts/idle_forge.sh
```

#### Aggressive (background server)
```bash
# Run unless CPU is very busy (<50% idle)
IDLE_CPU_THRESHOLD=50 CHECK_INTERVAL=120 ./scripts/idle_forge.sh
```

#### Check progress
```bash
# In another terminal
tail -f /tmp/musaeus_idle_forge.log

# Or check database directly
python3 -c "
from musaeus.config import get_config
from musaeus.db import open_db
cfg = get_config()
conn = open_db(cfg.db_path)
forged = conn.execute('SELECT COUNT(*) FROM archive WHERE rg_tagged_at IS NOT NULL').fetchone()[0]
total = conn.execute(\"SELECT COUNT(*) FROM archive WHERE status='CATALOGUED'\").fetchone()[0]
print(f'Progress: {forged:,}/{total:,} ({100*forged/total:.1f}%)')
conn.close()
"
```

### Stopping

```bash
# Ctrl+C in terminal (gracefully resumes forge before exit)
^C

# Or kill the monitor script (forge continues running)
pkill -f idle_forge.sh

# Manually resume forge if needed
kill -CONT $(pgrep -f "musaeus forge")
```

### Troubleshooting

**Script exits immediately:**
- Check that forge process exists: `pgrep -f "musaeus forge"`
- Check logs: `tail -50 /tmp/musaeus_idle_forge.log`

**Forge never starts:**
- Check CPU idle threshold: `top` (press '1' to see per-core)
- Lower threshold: `IDLE_CPU_THRESHOLD=50 ./scripts/idle_forge.sh`

**Forge keeps pausing:**
- Check CPU usage patterns
- Increase threshold: `IDLE_CPU_THRESHOLD=95 ./scripts/idle_forge.sh`
- Increase check interval: `CHECK_INTERVAL=120 ./scripts/idle_forge.sh`

---

## Adding More Scripts

When adding new utility scripts:

1. Make them executable: `chmod +x scripts/your_script.sh`
2. Add proper documentation header
3. Use environment variables for configuration
4. Log to `/tmp/` for debugging
5. Handle signals gracefully (trap INT/TERM)
6. Update this README

---

## Dependencies

- `bash` (>= 4.0)
- `top` (for CPU monitoring)
- `bc` (for float comparison)
- `pgrep`/`pkill` (for process management)

All should be available on standard Linux installations.
