# Musaeus AAC-Car-Masked Pipeline — Usage Guide

## Overview

The AAC-Car-Masked pipeline encodes your music library to AAC-Car (256k, -14 LUFS) and optionally mixes subliminal noise underneath for car listening ambiance. This is a three-stage optional export pipeline separate from the core archive.

**Status**: ✅ Fully implemented and tested

---

## Quick Start

### Generate Noise Samples (One-Time Setup)

```bash
musaeus noise-gen
```

Creates Pink/Brown/White noise samples (30min and 60min) at -16 LUFS in:
```
{vault}/RUNS/Noise/
  ├── Pink_Noise_30min.m4a
  ├── Brown_Noise_30min.m4a
  ├── White_Noise_30min.m4a
  ├── Pink_Noise_60min.m4a
  ├── Brown_Noise_60min.m4a
  └── White_Noise_60min.m4a
```

**Note**: Noise generation is compute-intensive (~8 min per 30min track on a modern CPU). Plan for ~1 hour total.

### Encode ALAC/FLAC → AAC-Car

```bash
musaeus aac-car [--workers N]
```

Encodes lossless source files to AAC-Car 256k with EBU R128 LUFS normalization (-14 LUFS target).

**Configuration**:
- Source: `{vault}/RUNS/ALAC_SOURCE/` or `MUSAEUS_ALAC_SOURCE_DIR` env var
- Output: `{vault}/RUNS/AAC-Car/BATCH_001/[Artist]/[Album]/[Track].m4a`
- Bitrate: 256k AAC
- Target LUFS: -14.0 (car profile, slightly louder than portable)
- Workers: 4 (default) — parallel ffmpeg jobs

**Example**:
```bash
musaeus aac-car --workers 8  # Use 8 parallel encoders
```

### Mix Noise Under Tracks

```bash
musaeus aac-car-mask [--noise PROFILE] [--workers N]
```

Layers noise underneath each AAC-Car track for subliminal car ambiance.

**Noise Profiles**:
| Profile | Mix | Levels | Use Case |
|---------|-----|--------|----------|
| `dual` (default) | Brown + Pink | -20dB + -24dB | Balanced road/highway ambiance |
| `brown` | Brown only | -20dB | Low-freq engine/road rumble |
| `pink` | Pink only | -24dB | Mid-range fill |
| `white` | White only | -28dB | High-freq wind hiss |
| `clean` | None | — | Copy AAC-Car unchanged (no noise) |

**Example**:
```bash
musaeus aac-car-mask --noise dual --workers 8
```

### Full Pipeline (One Command)

```bash
musaeus aac-car-full [--noise PROFILE] [--workers N]
```

Runs all three stages in sequence:
1. Generate noise samples (if not present)
2. Encode ALAC/FLAC → AAC-Car
3. Mix noise under tracks

**Example**:
```bash
musaeus aac-car-full --noise brown --workers 8
```

---

## Configuration

### Environment Variables

Set in `~/.config/musaeus/settings.env` or export:

```bash
# ALAC/FLAC source directory (optional)
export MUSAEUS_ALAC_SOURCE_DIR=/mnt/Music/Lossless

# Override output directories (optional)
export MUSAEUS_AAC_CAR_ROOT=/mnt/Music/AAC-Car
export MUSAEUS_AAC_CAR_MASKED_ROOT=/mnt/Music/AAC-Car-Masked
export MUSAEUS_NOISE_DIR=/mnt/Music/Noise
```

### Default Paths

```
Vault Root: {MUSAEUS_VAULT_ROOT}/RUNS/

ALAC Source:     ALAC_SOURCE/
Noise:           Noise/
AAC-Car:         AAC-Car/BATCH_001/[Artist]/[Album]/[Track].m4a
AAC-Car-Masked:  AAC-Car-Masked/[Artist]/[Album]/[Track].m4a
```

---

## Idempotency & Resuming

All stages are **idempotent** — re-running skips already-processed files:

```bash
# Skip existing noise samples, encode new FLAC files only
musaeus aac-car --workers 8

# Skip existing masked files, re-mask only new AAC-Car files
musaeus aac-car-mask --noise pink
```

---

## Performance Tips

### Parallel Encoding

The `--workers` flag controls parallel ffmpeg jobs. Recommended values:

| CPU Cores | Workers | Notes |
|-----------|---------|-------|
| 2 | 1–2 | Single-threaded or light parallelism |
| 4 | 4 | Match CPU count |
| 8 | 6–8 | Leave room for OS/other tasks |
| 16+ | 8–12 | Optimal throughput without resource contention |

**Example**:
```bash
musaeus aac-car --workers 8      # For an 8-core machine
musaeus aac-car-mask --workers 8
```

### Storage

AAC-Car files are typically 20–30% smaller than FLAC:
- 1GB of FLAC → ~250–300MB AAC-Car
- 1GB of AAC-Car + Masked → ~500–600MB total (noise adds ~50MB per track)

---

## Noise Philosophy

The noise levels are **barely audible** (subliminal) and designed to mask road/wind noise in a car:

- **Brown (-20dB)**: Low-frequency rumble (engine, road)
- **Pink (-24dB)**: Mid-range fill (frequency balance)
- **White (-28dB)**: High-frequency presence (wind hiss reduction)

The combined dual profile fills psychoacoustic gaps during quiet passages, preventing road/wind noise from becoming the dominant sound in silence.

**Before**: Drive in silence → road noise dominates quiet moments
**After**: Drive with dual noise → consistent, even ambiance without conscious noise awareness

---

## Workflow Example

### Typical Setup

```bash
# 1. Configure source ALAC directory
export MUSAEUS_ALAC_SOURCE_DIR=/home/grey/Music/Lossless/ALAC_Vault

# 2. One-time noise generation
musaeus noise-gen
# ⏱ ~1 hour for 6 noise tracks

# 3. Encode full library
musaeus aac-car --workers 8
# ⏱ Time depends on library size (50MB/min on modern CPU)

# 4. Mask with noise
musaeus aac-car-mask --noise dual --workers 8
# ⏱ ~2x encode time (amix + re-encode)

# 5. Export to USB for car
cp -r {vault}/RUNS/AAC-Car-Masked/* /mnt/USB/Music/
```

### Incremental Updates

After initial setup, music library updates flow like this:

```bash
# New FLAC files in source
musaeus aac-car --workers 8
# → Encodes only new files (skips existing)

# New AAC-Car files created
musaeus aac-car-mask --noise dual --workers 8
# → Masks only new files

# Sync to car
rsync -av --delete {vault}/RUNS/AAC-Car-Masked/ /mnt/USB/Music/
```

---

## Troubleshooting

### "Noise file not found"

Run noise generation first:
```bash
musaeus noise-gen
```

### "ALAC source not found"

Set `MUSAEUS_ALAC_SOURCE_DIR` or populate `{vault}/RUNS/ALAC_SOURCE/`:

```bash
export MUSAEUS_ALAC_SOURCE_DIR=/path/to/lossless/library
musaeus aac-car
```

### Slow encoding

Check CPU load and reduce `--workers`:
```bash
musaeus aac-car --workers 4  # Reduce parallelism
```

Or check disk I/O:
```bash
iotop  # Monitor disk usage
```

### Database locked

Close other musaeus processes:
```bash
pkill -f "python.*musaeus"
```

---

## Advanced: Custom Noise Profiles

To create a custom noise profile, edit the NOISE_PROFILES dict in `musaeus/stages/aac_car_masked.py`:

```python
NOISE_PROFILES = {
    "my_profile": [("brown", -18.0), ("white", -26.0)],  # Custom mix
    # ... existing profiles ...
}
```

Then use:
```bash
musaeus aac-car-mask --noise my_profile
```

---

## Integration with ORPHEUS

If migrating from ORPHEUS:

1. Set source to ORPHEUS ALAC vault:
```bash
export MUSAEUS_ALAC_SOURCE_DIR=/mnt/FORGE2TB/Projects/ORPHEUS/RUNS/Music.Vault/ALAC
```

2. Run full pipeline:
```bash
musaeus aac-car-full --noise dual --workers 8
```

3. Compare output quality to ORPHEUS AAC-Car-Masked
4. Deprecate ORPHEUS scripts once validation passes

---

## See Also

- Design document: `AAC_CAR_MASKED_DESIGN.md`
- ORPHEUS equivalent: `build_aac_car_masked.py` (original implementation)
- Core stages: `musaeus/stages/noise_generator.py`, `aac_car.py`, `aac_car_masked.py`
