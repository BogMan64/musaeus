# Musaeus AAC-Car-Masked Feature Design

## Overview
Add a complete AAC-Car-Masked pipeline to Musaeus, mirroring ORPHEUS functionality:
1. Encode ALAC vault → AAC-Car (256k, -14 LUFS)
2. Mix subliminal noise under each track (Brown -20dB / Pink -24dB / White -28dB)
3. Build genre playlists from the masked output

This is an **optional export pipeline** separate from the core Forge/Tagger/Curator stages. It processes files already in the archive or from a source ALAC vault.

---

## Architecture & Integration

### New Stages (musaeus/stages/)

#### 1. **NoiseGeneratorStage** (`noise_generator.py`)
- **Purpose**: Generate Pink/Brown/White noise 30min + 60min tracks at -16 LUFS
- **Input**: None (generates from scratch using ffmpeg lavfi)
- **Output**: `{runs_root}/Noise/[Pink/Brown/White]_Noise_[30/60]min.m4a`
- **Idempotent**: Yes — checks if files exist, skips if present
- **Dry-run**: Lists what would be generated
- **Config**:
  - `noise_target_lufs`: -16.0 (fixed, matches Orpheus)
  - `noise_bitrate`: "256k"
  - `noise_sample_rate`: 44100

#### 2. **AACCarStage** (`aac_car.py`)
- **Purpose**: Encode ALAC source → AAC-Car 256k with 2-pass LUFS normalization
- **Input**: Files from `{runs_root}/ALAC_SOURCE/` or archive FORGED tracks (if available)
- **Output**: `{runs_root}/AAC-Car/BATCH_001/[Artist]/[Album]/[Track].m4a`
- **Idempotent**: Yes — skips already-encoded files
- **Parallel**: ThreadPoolExecutor with `--workers` (default: 4)
- **Metadata**: Inherits tags from source, logs cleaning/naming events
- **Dry-run**: Lists files to encode without running ffmpeg
- **Config**:
  - `aac_car_bitrate`: "256k"
  - `aac_car_target_lufs`: -14.0
  - `aac_car_workers`: 4

#### 3. **AACCarMaskedStage** (`aac_car_masked.py`)
- **Purpose**: Mix noise under each AAC-Car track
- **Input**: AAC-Car files from previous stage (or existing AAC-Car vault)
- **Output**: `{runs_root}/AAC-Car-Masked/[Artist]/[Album]/[Track].m4a`
- **Idempotent**: Yes — skips already-masked files
- **Parallel**: ThreadPoolExecutor with `--workers` (default: 4)
- **Noise Profile**: Selectable mix (dual, pink, brown, white, clean)
  - `dual` (default): Pink + Brown
  - `pink`: Pink only
  - `brown`: Brown only
  - `white`: White only
  - `clean`: No noise (AAC-Car direct copy)
- **Dry-run**: Lists files to mask without running ffmpeg
- **Config**:
  - `aac_car_masked_brown_db`: -20.0 (subliminal)
  - `aac_car_masked_pink_db`: -24.0
  - `aac_car_masked_white_db`: -28.0
  - `aac_car_masked_workers`: 4
  - `aac_car_masked_noise_profile`: "dual"

---

### Config & Paths (config.py)

Add to `MusicConfig`:
```python
# AAC-Car-Masked export
aac_car_root: Path         # {runs_root}/AAC-Car
aac_car_masked_root: Path  # {runs_root}/AAC-Car-Masked
noise_dir: Path            # {runs_root}/Noise
alac_source_dir: Path      # {runs_root}/ALAC_SOURCE (optional, for legacy migr.)
```

Add environment variables:
- `MUSAEUS_AAC_CAR_ROOT` (default: `{runs_root}/AAC-Car`)
- `MUSAEUS_AAC_CAR_MASKED_ROOT` (default: `{runs_root}/AAC-Car-Masked`)
- `MUSAEUS_NOISE_DIR` (default: `{runs_root}/Noise`)
- `MUSAEUS_ALAC_SOURCE_DIR` (default: empty — auto-discover)

---

### CLI Commands (cli.py)

```bash
musaeus noise-gen [--apply]                    # Generate noise samples
musaeus aac-car [--apply] [--workers N]        # Encode ALAC → AAC-Car
musaeus aac-car-mask [--apply] [--noise PROFILE] [--workers N]
                                               # Mix noise under AAC-Car
musaeus aac-car-full [--apply] [--noise PROFILE] [--workers N]
                                               # Run all three stages
```

**Flags**:
- `--apply`: Execute (default: dry-run)
- `--force`: Re-process already-done files
- `--workers N`: Parallel jobs (default: 4)
- `--noise {dual|pink|brown|white|clean}`: Noise profile for masking
- `--limit N`: Cap number of files (testing)

---

### Console Menu Integration (console.py)

Add new options to interactive menu:
```
  ...existing options...
  11  AAC-Car export (3-stage pipeline)
      a) Generate noise samples
      b) Encode ALAC → AAC-Car
      c) Mix noise (masking)
      d) Run full pipeline (a→b→c)
  12  Quit
```

---

### Database Integration

**Minimal DB changes**: Add optional columns to `archive` table for tracking:
- `aac_car_path`: NULL or path to AAC-Car export
- `aac_car_masked_path`: NULL or path to masked export
- `aac_car_masked_at`: Timestamp of last masking
- `aac_car_noise_profile`: "dual" | "pink" | "brown" | "white" | "clean"

**Live migration**: Schema auto-updates on `open_db()` (existing pattern).

---

### File Structure

```
musaeus/
├── stages/
│   ├── noise_generator.py     ← NEW: Generate noise
│   ├── aac_car.py             ← NEW: Encode ALAC→AAC-Car
│   ├── aac_car_masked.py      ← NEW: Mix noise under tracks
│   └── (existing stages...)
├── cli.py                      ← ADD: noise-gen, aac-car, aac-car-mask, aac-car-full cmds
├── console.py                  ← ADD: Menu options 11a–11d
└── config.py                   ← ADD: aac_car_root, etc.
```

---

## Implementation Strategy

### Phase 1: Foundation
- [ ] Update `config.py` with new paths + env vars
- [ ] Create `noise_generator.py` stage

### Phase 2: Encoding
- [ ] Create `aac_car.py` stage
- [ ] Test encode pipeline (dry-run + apply)

### Phase 3: Masking
- [ ] Create `aac_car_masked.py` stage with ffmpeg amix logic
- [ ] Test masking pipeline (dry-run + apply)

### Phase 4: Integration
- [ ] Add CLI commands (`cli.py`)
- [ ] Add console menu (`console.py`)
- [ ] Update database schema (live migration)

### Phase 5: Testing
- [ ] End-to-end test with small file set
- [ ] Verify noise levels with ffprobe analysis
- [ ] Compare output to ORPHEUS implementation

---

## Key Design Decisions

1. **Stages are independent**: Can run noise-gen, aac-car, or aac-car-masked separately or together
2. **Idempotent by default**: Rerunning skips already-processed files unless `--force`
3. **Minimal DB coupling**: AAC-Car-Masked is an **optional export**, not part of core archive
4. **Noise files centralized**: All noise tracks in `{runs_root}/Noise/`, reused across runs
5. **Parallelism**: ThreadPoolExecutor for ffmpeg efficiency (default 4 workers)
6. **Dry-run safety**: Legacy dry-run disabled, but stages implement custom `validate()` checks

---

## Notes for Implementation

- **ffmpeg requirements**: `anoisesrc` filter (available in modern ffmpeg)
- **Noise masking filter**: `amix=inputs=2:duration=longest` to layer noise + track
- **Tag preservation**: Copy all ID3/Vorbis/iTunes tags from source to output
- **Error handling**: Log ffmpeg stderr; mark individual files as failed but continue batch
- **Progress reporting**: Print per-file results to console (match ORPHEUS style)

---

## Migration Path from ORPHEUS

1. Run `musaeus noise-gen --apply` to create Musaeus noise inventory
2. Point `MUSAEUS_ALAC_SOURCE_DIR` at ORPHEUS ALAC vault
3. Run `musaeus aac-car-full --apply` to encode + mask
4. Compare output quality to ORPHEUS AAC-Car-Masked
5. Deprecate ORPHEUS scripts once validation passes
