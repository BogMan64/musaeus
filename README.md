# Musaeus

**Music Library Pipeline** — the student of Orpheus, keeper of sacred knowledge.

Musaeus is a clean-room music library management system built as a successor to ORPHEUS.
It ingests audio files, hashes them content-addressably, extracts metadata, measures loudness,
writes ReplayGain tags, exports a car library, and helps you resolve duplicates — all from a
single `musaeus` command.

---

## Quick Start

```bash
# 1. Drop audio files into your inbox
cp ~/Downloads/*.flac /mnt/FORGE2TB/Projects/MUSAEUS_VAULT/INBOX/

# 2. Run the full pipeline
musaeus run

# 3. Check status
musaeus status

# 4. Launch the interactive console
musaeus
```

---

## Installation

```bash
cd /mnt/FORGE2TB/Projects/MUSAEUS
pip3 install --user -e .
```

**Dependencies:**
- Python 3.10+
- `ffmpeg` + `ffprobe` (required for hashing and loudness)
- `mutagen` — tag writing (`pip install mutagen`)
- `rapidfuzz` — fuzzy canon matching (`pip install rapidfuzz`) *(optional)*

**Configuration** (`~/.config/musaeus/settings.env`):
```env
MUSAEUS_VAULT_ROOT=/mnt/FORGE2TB/Projects/MUSAEUS_VAULT
MUSAEUS_DB_PATH=/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/musaeus.db
GROQ_API_KEY=gsk_...          # optional — AI features
```

---

## Vault Layout

```
MUSAEUS_VAULT/
├── INBOX/          ← drop audio files here
├── STAGING/        ← temporary working area
├── QUARANTINE/     ← files moved here by problem detection
├── RUNS/           ← per-run logs and reports
├── Noise/          ← optional noise tracks for car export
│   ├── Pink_Noise_30min.m4a
│   └── Brown_Noise_60min.m4a
├── MetaData/
│   ├── artist_canon.tsv     ← artist name normalisation map
│   ├── genre_allowed.txt    ← allowed genre list
│   └── genre_map.tsv        ← raw genre → canonical genre
└── musaeus.db      ← SQLite database (WAL mode)
```

---

## Pipeline

Files move through these states:

```
INBOX file dropped
      ↓
  INGEST    → PENDING      (registered in DB)
      ↓
  SENTINEL  → HASHED       (audio hash computed, exact dupes detected)
      ↓
  SCHOLAR   → CATALOGUED   (ffprobe metadata extracted)
      ↓
  FORGE     → rg_tagged    (EBU R128 LUFS measured, ReplayGain tags written)
      ↓
  TAGGER                   (DB metadata written back to file tags)
      ↓
  CURATOR   → car_export   (car-library copy built)
```

**On-demand maintenance stages** (run separately or via `musaeus run --maintain`):

```
  GHOST     → status=GHOST  (archive rows whose files no longer exist)
  HEALTH    → validation_issues table  (missing tags, bad bitrate, etc.)
  ENRICH    → genre filled  (Last.fm top tag → GenreCanon resolution)
  NEARDUPE  → duplicates table  (fuzzy title match within artist group)
```

Each stage is idempotent — re-running skips already-processed files.

---

## CLI Reference

### Pipeline Commands

```bash
musaeus run              # Ingest → Sentinel → Scholar
musaeus run --full       # + Forge → Tagger
musaeus run --maintain   # Ghost + Health + Enrich + NearDupe

musaeus ingest           # Register new INBOX files
musaeus sentinel         # Hash files, detect exact duplicates
musaeus scholar          # Extract ffprobe metadata
musaeus forge            # Measure LUFS + write ReplayGain tags
musaeus forge --force    # Re-tag already-forged files
musaeus tagger           # Write normalised DB metadata back to tags
musaeus curator --export-root /mnt/USB --noise dual
```

> **Preview status:** Legacy preview/dry-run is temporarily unavailable because it can
> persist state. `musaeus dry-run`, every `--dry-run` CLI route, interactive-console
> preview selection, and legacy script dry-run/default-preview routes fail closed before
> MUSAEUS starts managed configuration, database, library/file, log, or network work
> for that preview. Run without `--dry-run` only for an explicitly authorised live
> run, or wait for the safe-preview repair. Low-level `BaseStage` APIs are internal
> execution interfaces and must not be used as a consumer preview safety boundary.

### Maintenance Commands

```bash
musaeus ghost            # Sweep archive for files missing from disk

musaeus health           # Run consistency + quality checks
musaeus health-report    # Print validation issues summary table

musaeus enrich           # Fill missing genres via Last.fm

musaeus neardupe         # Detect near-duplicate tracks (fuzzy title match)
```

### Review Commands

```bash
musaeus status           # Library overview (+ ghost/health counts)
musaeus runs             # List recent pipeline runs
musaeus dedupe           # Interactive duplicate review (EXACT + NEAR)
musaeus dedupe --auto    # Auto-resolve: keep highest quality
musaeus dedupe --report  # Show duplicate summary (no changes)
musaeus health-report    # Print issue breakdown by type + worst files
```

### Noise Profiles (Curator)

| Mode    | Tracks included                    |
|---------|------------------------------------|
| `clean` | No noise tracks                    |
| `pink`  | Pink_Noise_*.m4a                   |
| `brown` | Brown_Noise_*.m4a                  |
| `white` | White_Noise_*.m4a                  |
| `dual`  | Pink + Brown  *(default)*          |

Place noise files in `<vault>/Noise/` before running curator.

---

## Interactive Console

```bash
musaeus          # launches the console
```

```
╔══════════════════════════════════════════════════════════╗
║  MUSAEUS  v0.1.0  —  Music Library Pipeline              ║
╚══════════════════════════════════════════════════════════╝

    0  Status
    1  Preview temporarily unavailable  ← safety block + remediation
    2  Run full pipeline  [LIVE]
    3  Run single stage…       ← Ingest/Sentinel/Scholar/Forge/Tagger/Curator
    4  Dedupe review           ← Interactive / Auto / Report
    5  View recent runs
    6  Inspect a run           ← shows run IDs before prompting
    7  View duplicates
    8  Configuration
    9  Reset / fresh start     ← Soft reset or Hard reset
   10  Quit
```

### Reset Options (option 9)

| Mode         | What it does                                                  | Confirmation |
|--------------|---------------------------------------------------------------|--------------|
| Soft reset   | Re-queues all files as PENDING, keeps event log and history   | Type `YES`   |
| Hard reset   | Deletes the entire database (blank slate)                     | Type `DELETE` twice |

> Files in INBOX are **never deleted** by either reset — only the database is affected.

---

## Duplicate Detection

### Exact Duplicates (Sentinel stage)
The Sentinel stage detects exact duplicates by comparing content-addressed hashes
(SHA-256 of the decoded PCM stream — format-agnostic).
Re-tagging a file changes its full_hash but not its audio_hash — no false positive.

### Near Duplicates (NearDupe stage)
The NearDupe stage finds tracks from the same artist with very similar titles:
- Groups tracks by canonical artist name (ArtistCanon fuzzy ≥88)
- Normalises titles: lowercase, strip punctuation, collapse whitespace, strip "The"
- Flags pairs with `fuzz.ratio ≥ 88` — conservative to avoid false positives
- Stages results in `duplicates` table with `type='NEAR'` and a confidence score

Groups are stored in the `duplicates` table with types:
- `EXACT` — identical audio stream (different file, container, or tags)
- `NEAR` — very similar title from same artist (NearDupe stage)
- `CROSS_FORMAT` — same audio in different formats (e.g. FLAC + M4A)

**Resolve duplicates:**
```bash
musaeus dedupe           # interactive: type 1k to keep item 1, 2a to archive item 2
musaeus dedupe --auto    # keeps highest bitrate/size, archives rest
musaeus dedupe --report  # summary table only
```

---

## Canon System

Musaeus normalises raw metadata strings to canonical forms.

### Artist Canon (`MetaData/artist_canon.tsv`)

```tsv
# raw_name<TAB>canonical_name
the beatles	The Beatles
portishead 	Portishead
```

- Exact match (case-insensitive) → first lookup
- Fuzzy match (rapidfuzz ratio ≥ 88) → fallback
- Unmatched → returned as-is

### Genre Canon

**`MetaData/genre_allowed.txt`** — one allowed genre per line:
```
Alternative
Electronic
Hip-Hop
Jazz
```

**`MetaData/genre_map.tsv`** — raw → canonical overrides:
```tsv
Hip-Hop/Rap	Hip-Hop
Trip Hop	Electronic
```

Resolution order: exact allowed-list match → explicit map → fuzzy match (≥82) → `None`

---

## Database

SQLite with WAL mode. Path: `$MUSAEUS_DB_PATH` or `<vault>/musaeus.db`.

**Key tables:**

| Table              | Purpose                                              |
|--------------------|------------------------------------------------------|
| `archive`          | One row per known file — current state               |
| `events`           | Immutable append-only event log (source of truth)    |
| `duplicates`       | Detected duplicate groups pending review             |
| `validation_issues`| Issues flagged during processing                     |
| `metadata_cache`   | Raw ffprobe JSON output                              |

**Archive columns include:**
`file_path`, `audio_hash`, `full_hash`, `artist`, `album`, `title`, `genre`, `year`,
`track`, `duration`, `bitrate`, `codec`, `status`,
`lufs`, `lufs_tp`, `rg_gain`, `rg_peak`, `rg_tagged_at`,
`car_export_path`, `noise_profile`

**Live migrations** — new columns are added automatically on `open_db()`.
You never need to run manual migrations.

---

## ReplayGain / Loudness

Forge uses **EBU R128** via `ffmpeg loudnorm`:

- Reference: **−18 LUFS** (home listening middle-ground)
- Apple Music target: −16 LUFS
- Spotify target: −14 LUFS
- EBU R128 broadcast: −23 LUFS

Tags written per format:

| Format | Tag                              |
|--------|----------------------------------|
| M4A    | `com.apple.iTunes.R128_TRACK_GAIN` (Q7.8 fixed-point) |
| FLAC   | `REPLAYGAIN_TRACK_GAIN` + `REPLAYGAIN_TRACK_PEAK` |
| MP3    | `replaygain_track_gain` (EasyID3) |
| AIFF   | `TXXX:replaygain_track_gain` (ID3)|
| WAV    | DB-only (no standard tag container) |

Forge has a 120-second per-file timeout (threading timer) to prevent hung ffmpeg processes.
Already-forged files are skipped unless you run `musaeus forge --force`.

---

## Architecture

```
musaeus/
├── __init__.py         version
├── __main__.py         python -m musaeus entry point
├── cli.py              argparse CLI
├── console.py          interactive terminal UI
├── config.py           MusicConfig — paths, env vars
├── context.py          RunContext — shared run state
├── db.py               open_db(), schema, live migrations
├── hasher.py           content-addressed audio hashing (ffmpeg PCM)
├── fuzzy.py            fuzzy match helpers
├── loudness.py         EBU R128 measurement (ffmpeg loudnorm)
├── dedupe.py           duplicate review console
├── canon/
│   ├── artist.py       ArtistCanon (TSV + fuzzy)
│   └── genre.py        GenreCanon (allowed list + map + fuzzy)
└── stages/
    ├── base.py         BaseStage ABC
    ├── ingest.py       IngestStage      — scan inbox, register files
    ├── sentinel.py     SentinelStage    — hash, exact dupe detection
    ├── scholar.py      ScholarStage     — ffprobe metadata extraction
    ├── forge.py        ForgeStage       — EBU R128 loudness + ReplayGain
    ├── tagger.py       TaggerStage      — write DB metadata back to tags
    ├── curator.py      CuratorStage     — car-library export + noise
    ├── ghost.py        GhostStage       — mark files missing from disk
    ├── health.py       HealthStage      — consistency + quality checks
    ├── enrich.py       EnrichStage      — Last.fm genre enrichment
    └── neardupe.py     NearDupeStage    — fuzzy title near-dupe detection
```

**Design principles:**
- One `RunContext` per pipeline run — no global state
- Event log as source of truth — DB is always rebuildable
- Every stage implements `run()`, `dry_run()`, `validate()`
- Stages never commit the DB — `ctx.record_stage()` does
- Periodic commits every N files (crash resilience)
- Legacy `dry_run` CLI/console preview is temporarily unavailable pending the safe-preview repair

---

## Run IDs

Every pipeline run gets a unique ID: `run_20260710T062121Z_b7e4d2`

Find yours:
```bash
musaeus runs           # list last 20
```
Or in the console: option 5 (View recent runs) or option 6 (Inspect a run — shows list automatically).

---

## GitHub

```
https://github.com/BogMan64/musaeus  (private)
```

---

*Named for Musaeus of Athens — student of Orpheus, mythological keeper of sacred songs.*
