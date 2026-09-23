---
name: verify
description: Build, launch and drive MUSAEUS to observe a change at its real surface.
---

# Verifying MUSAEUS at runtime

No build step. The package is installed editable and already points at this
checkout — confirm with:

    python3 -c "import musaeus; print(musaeus.__file__)"

## Use `python3 -m musaeus`, not the `musaeus` console script

The console script at `~/.local/bin/musaeus` does NOT put the repo root on
`sys.path`. Every CLI command whose body does `from scripts.X import ...`
(`report`, `spec-scout`, `upgrade-check`, `canon-review`) therefore dies with
`ModuleNotFoundError: No module named 'scripts'` — including when cwd IS the
repo root. `scripts/` has no `__init__.py` and pyproject's
`packages.find include = ["musaeus*"]` leaves it out of the installed dist.

`python3 -m musaeus <cmd>` works, because `-m` puts cwd on `sys.path`.
Run it from the repo root.

## Never drive against the real vault

`MUSAEUS_VAULT_ROOT` is the only path var set in
`~/.config/musaeus/settings.env`; everything else derives from it, and a
process env var always wins over the settings file. So one export redirects
the whole vault. Build a throwaway one instead of touching the ~468 GB
library:

    F=$SCRATCH/FIX
    mkdir -p "$F"/{MetaData,Libraries/ALAC_Archive,INBOX,RUNS,QUARANTINE}
    cp $VAULT/MetaData/{Genre_Allowed.txt,Genre_Canonical_Map.txt} "$F/MetaData/"
    sqlite3 "$F/musaeus.db" "$(sqlite3 $VAULT/musaeus.db '.schema archive')"
    export MUSAEUS_VAULT_ROOT="$F" MUSAEUS_RUNS_ROOT="$F/RUNS" \
           MUSAEUS_ALAC_ARCHIVE="$F/Libraries/ALAC_Archive"

Override `MUSAEUS_RUNS_ROOT` even for read-only reporting commands —
`spec-scout`, `upgrade-check` and `canon-review report` all write a report
file into `RUNS/`.

## Fixture audio

Use **noise, not a tone**. A sine wave compresses to almost nothing as ALAC
and trips CorruptStage's size-ratio check, so you end up verifying the wrong
branch.

    ffmpeg -f lavfi -i "anoisesrc=d=177:c=pink:r=44100:a=0.5" -ac 2 -c:a alac out.m4a

To make a genuinely damaged file (a real decode failure), `truncate` a valid
one — it loses the moov atom and ffmpeg rejects it.

## Gotchas

- `musaeus corrupt --dry-run` prints "no preview available for this stage".
  Dry-run tells you nothing here; drive it for real against the fixture.
- CorruptStage only quarantines on a **failed decode**. Any check that merely
  flags a file (short duration, size ratio, near-zero, short-relative-to-
  sibling) is routed through `ffmpeg_decode_check` first, and a complete
  short file decodes cleanly and is cleared. Test such checks by running the
  stage, not by calling `check_file`.
- `MasterLaw.csv` is appended to in place by `scripts/itunes_genre_fill.py`.
  Snapshot it before driving that script.
