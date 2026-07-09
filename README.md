# MUSAEUS

**Music Library Pipeline Framework** — clean-room reference implementation of ORPHEUS/NexusII principles.

> *Musaeus was the student of Orpheus — keeper of sacred knowledge, translator of the master's wisdom into written form.*

---

## Philosophy

Musaeus is what happens when you take the best ideas from ORPHEUS and NexusII, throw away the accumulated cruft, and build from first principles:

| Principle | Implementation |
|-----------|---------------|
| **One RunContext** | Single shared state object per run. No global vars. |
| **Event log first** | Every mutation is appended to `events`. DB is derived, always rebuildable. |
| **Content-addressed audio** | Hash only the PCM stream — re-tagging never changes identity. |
| **Config via env** | Zero hardcoded paths. Move vault → change one env var. |
| **dry_run is mandatory** | Every stage MUST implement `dry_run()`. No exceptions. |
| **No module-level side effects** | No `mkdir`, `basicConfig`, or I/O at import time. |

---

## Quick Start

```bash
# 1. Set your vault location
export MUSAEUS_VAULT_ROOT=/path/to/your/music/vault

# (or add to ~/.config/musaeus/settings.env)
echo "MUSAEUS_VAULT_ROOT=/path/to/your/music/vault" >> ~/.config/musaeus/settings.env

# 2. Install
pip install -e ".[fuzzy]"         # includes rapidfuzz

# 3. Drop files into the inbox
mkdir -p /path/to/your/music/vault/INBOX
cp *.flac /path/to/your/music/vault/INBOX/

# 4. Preview what will happen (safe — no mutations)
musaeus dry-run

# 5. Run for real
musaeus run

# 6. Or use the interactive console
musaeus console
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MUSAEUS_VAULT_ROOT` | **(required)** | Root of your music vault |
| `MUSAEUS_DB_PATH` | `{VAULT}/musaeus.db` | Override DB location |
| `MUSAEUS_INBOX` | `{VAULT}/INBOX` | New files arrive here |
| `MUSAEUS_STAGING` | `{VAULT}/STAGING` | Pre-vault staging area |
| `MUSAEUS_QUARANTINE` | `{VAULT}/QUARANTINE` | Bad files go here |
| `MUSAEUS_RUNS_ROOT` | `{VAULT}/RUNS` | Run logs and reports |
| `MUSAEUS_META_DIR` | `{VAULT}/MetaData` | Canon CSVs |
| `GROQ_API_KEY` | *(optional)* | Groq AI inference |
| `LASTFM_API_KEY` | *(optional)* | Last.fm genre enrichment |
| `OPENROUTER_API_KEY` | *(optional)* | OpenRouter API |

Config files (loaded in priority order, env vars always win):
1. `~/.config/musaeus/settings.env`
2. `~/.config/musaeus/credentials.env`
3. `{project_root}/.env`

---

## Pipeline Stages

```
INBOX  →  [Ingest]  →  [Sentinel]  →  [Scholar]  →  CATALOGUED
```

### Stage 1: Ingest
Scans `INBOX` recursively. Registers new audio files in the `archive` table with `status=PENDING`. Idempotent — re-running is always safe.

### Stage 2: Sentinel
Computes two hashes per file:
- **`audio_hash`** — SHA-256 of the raw PCM audio stream (tags irrelevant)
- **`full_hash`** — SHA-256 of the entire file (for change detection)

Detects `EXACT` duplicates (same `audio_hash`). Stages them in `duplicates` for review.

### Stage 3: Scholar
Runs `ffprobe` on each `HASHED` file. Extracts title, artist, album, genre, year, track, duration, bitrate (always `INTEGER`), sample rate, channels, codec. Stores raw JSON in `metadata_cache`. Advances status to `CATALOGUED`.

---

## CLI

```bash
musaeus run              # full pipeline, live
musaeus dry-run          # full pipeline, preview only
musaeus ingest           # ingest stage only
musaeus ingest --dry-run # preview ingest
musaeus sentinel         # hash + dupe detect
musaeus scholar          # metadata extraction
musaeus status           # library statistics
musaeus runs             # list recent pipeline runs
musaeus console          # interactive TUI console
musaeus --version        # print version
```

---

## Interactive Console

```
musaeus console
```

The console provides a numbered menu for all pipeline operations:

```
╔══════════════════════════════════════════════════════════╗
║  MUSAEUS  v0.1.0  —  Music Library Pipeline              ║
╚══════════════════════════════════════════════════════════╝

  ▸ System Check
  ──────────────────────────────────────────────────────────
  ✓ Vault     : /vault
  ✓ Inbox     : /vault/INBOX
  ✓ DB        : /vault/musaeus.db
  ✓ ffmpeg    : found
  ✓ ffprobe   : found

  0  Status
  1  Run full pipeline  [DRY RUN]
  2  Run full pipeline  [LIVE]
  3  Run single stage…
  4  View recent runs
  5  Inspect a run
  6  View duplicates
  7  Configuration
  8  Quit
```

---

## Database Schema

All tables in `musaeus.db`:

| Table | Purpose |
|-------|---------|
| `events` | **Immutable event log** — source of truth |
| `archive` | Materialized state of every known file |
| `duplicates` | Staged duplicate pairs awaiting review |
| `validation_issues` | Issues flagged by validation (UNIQUE per file+issue+run) |
| `metadata_cache` | Raw ffprobe JSON + parsed fields |

The DB is **always rebuildable** from the event log. Treat it as a derived artifact.

---

## Architecture Notes

### Content-addressed hashing

Musaeus hashes the *decoded PCM audio stream*, not the container file. This means:

- Re-tagging a file (changing ID3 tags) → `full_hash` changes, `audio_hash` unchanged → **not a duplicate**
- Same audio in FLAC vs AAC → different `audio_hash` → **correctly identified as different quality**
- Exact copy with different filename → same `audio_hash` → **EXACT duplicate detected**

### Stage protocol

Every stage implements three methods:

```python
class MyStage(BaseStage):
    NAME = "my_stage"

    def validate(self, ctx):  # pre-flight, raise StageError if bad
    def run(self, ctx):       # real work, may mutate files/DB
    def dry_run(self, ctx):   # preview only, ZERO mutations
```

`dry_run()` is **never** a no-op. If a stage genuinely can't preview, that's a design smell.

### Lessons from ORPHEUS/NexusII

Issues avoided by design:

- ✗ `NEXUS_ROOT = Path("/mnt/FORGE2TB/NexusII")` hardcoded in 15 scripts → ✓ `MUSAEUS_VAULT_ROOT` env var
- ✗ `bitrate` stored as string → ✓ always `INTEGER` in schema
- ✗ `--dry-run` flag declared but ignored → ✓ mandatory `dry_run()` in ABC
- ✗ `loudnorm` single-pass (wrong EBU R128) → ✓ documented 2-pass requirement
- ✗ Hash entire file including tags → ✓ audio-stream-only PCM hash
- ✗ `logging.basicConfig()` at module level → ✓ only in entry points
- ✗ `shell=True` in subprocess → ✓ always list args, never shell
- ✗ No `UNIQUE` constraint on validation issues → ✓ `UNIQUE(file_path, issue, run_id)`

---

## Development

```bash
pip install -e ".[dev,fuzzy]"
pytest
ruff check musaeus/
ruff format musaeus/
```

---

## Project Structure

```
MUSAEUS/
├── musaeus/
│   ├── __init__.py       — package + version
│   ├── config.py         — MusicConfig, env loading, zero hardcodes
│   ├── context.py        — RunContext, StageResult
│   ├── db.py             — SQLite schema, open_db(), log_event(), upsert_archive()
│   ├── hasher.py         — audio_hash() (PCM SHA-256), file_hash()
│   ├── fuzzy.py          — normalize(), similarity(), is_match()
│   ├── console.py        — interactive TUI console
│   ├── cli.py            — CLI entry point (musaeus command)
│   ├── stages/
│   │   ├── __init__.py   — DEFAULT_PIPELINE
│   │   ├── base.py       — BaseStage ABC, StageError
│   │   ├── ingest.py     — Stage 1: register inbox files
│   │   ├── sentinel.py   — Stage 2: hash + dupe detect
│   │   └── scholar.py    — Stage 3: ffprobe metadata
│   └── canon/            — reserved for canon CSV management
├── tests/
│   ├── test_fuzzy.py
│   ├── test_hasher.py
│   ├── test_ingest.py
│   └── fixture/          — tiny test audio files
├── pyproject.toml
└── README.md
```
