# Musaeus

**Music library pipeline** — the student of Orpheus, keeper of sacred songs.

Musaeus takes raw rips and downloads and turns them into one clean library.
It catalogues every file in SQLite, sets duplicates aside by a fixed keep
rule, converts what is left to ALAC (or AAC for lossy sources), names, tags
and files it as a **master**, and builds three **editions** from the masters:
a lossless library, a car library and an iPhone library. It also checks the
masters for bit rot and backs them up.

It is one person's tool, built for one machine and about 8,000 songs. It is
the successor to ORPHEUS.

> **`musaeus run` changes files at once.** It moves files out of INBOX,
> converts them (the original of a converted file is held under
> `RUNS/recovery/` for the ten newest runs that converted something, then
> removed), and sets duplicates aside. Work on copies, start small, and read
> [docs/TESTING_ON_YOUR_OWN_FILES.md](docs/TESTING_ON_YOUR_OWN_FILES.md)
> before a first real run.

---

## Quick start

```bash
musaeus setup                 # choose a vault folder; optional API keys
cp ~/Downloads/*.flac /path/to/vault/INBOX/      # COPIES of your files
musaeus preflight             # is everything it needs installed?
musaeus run --dry-run         # preview (see "Known limitations")
musaeus run                   # do it
musaeus status                # what the catalogue holds
musaeus doctor                # read-only check: catalogue against disk
musaeus edition-build lossless   # then: car, iphone
musaeus                       # the menu
```

---

## Install

Linux only for now; developed and used on Debian 12 with Python 3.11. CI
runs Python 3.10, 3.11 and 3.12. A Windows port is partly done and paused.

### From source

```bash
git clone https://github.com/BogMan64/musaeus.git
cd musaeus
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[fuzzy]"
```

Use a virtual environment: Debian 12 refuses `pip3 install --user` with
"externally-managed-environment". Keep the `-e`: four commands (`report`,
`upgrade-check`, `spec-scout`, `canon-review`) load helpers from `scripts/`
in the checkout, which a plain `pip install .` does not copy.

### Debian package

`packaging/build-deb.sh` builds `musaeus_0.1.0-1_all.deb` from a checkout
(see [packaging/README.debian.md](packaging/README.debian.md)). No release
has been published on GitHub yet, so there is nothing to download.

```bash
sudo apt install ./musaeus_0.1.0-1_all.deb
```

The package does not pull in `rapidfuzz`; see "What it needs".

### Docker

For trying Musaeus on another machine. The image pins Debian 12, Python
3.11, ffmpeg 5.1 and fpcalc 1.5.1. It has no `fdkaac`, so it cannot build
the car and iPhone editions.

```bash
MUSAEUS_UID=$(id -u) MUSAEUS_GID=$(id -g) docker compose build
mkdir -p vault/INBOX && cp /some/music/*.flac vault/INBOX/   # COPIES
docker compose run --rm musaeus run --dry-run
```

The database lives on a Docker named volume, not in `vault/`: SQLite's WAL
mode needs locking that Docker Desktop's bind mounts do not reliably give.

---

## What it needs

| Need | For | Without it |
|---|---|---|
| `ffmpeg`, `ffprobe` | everything that reads audio | nothing works |
| `mutagen` (Python) | tags | installed with the package |
| `rapidfuzz` (Python, extra `fuzzy`) | near-duplicate detection, fuzzy name matching | the `neardupe` stage fails, every `run` ends with exit code 1, and a lossy copy of a song you already hold is filed as a second master |
| `essentia` (Python, extra `bpm`) | BPM, key, energy | the `bpm` stage fails and every `run` ends with exit code 1; use `musaeus run --skip bpm` |
| `fpcalc` (`libchromaprint-tools`) | AcoustID fingerprints | no acoustic duplicate detection |
| `fdkaac` | car and iPhone editions | those two builds fail |
| `ifuse`, `rsync` | `musaeus iphone-sync` | no phone copy |
| `rich`, `psutil` (Python) | progress bars, memory figure | plain output |

`musaeus preflight` lists what is missing and the package that provides it.

---

## Configuration

`musaeus setup` writes two files in `~/.config/musaeus/`:

- `settings.env` — paths.
- `credentials.env` — API keys, mode 600.

Both are `KEY=VALUE` files. A variable in the environment wins over both.

| Setting | Default | Meaning |
|---|---|---|
| `MUSAEUS_VAULT_ROOT` | none, **required** | the vault folder |
| `MUSAEUS_DB_PATH` | `<vault>/musaeus.db` | the catalogue |
| `MUSAEUS_INBOX` | `<vault>/INBOX` | where new files arrive |
| `MUSAEUS_STAGING` | `<vault>/STAGING` | working area for conversion |
| `MUSAEUS_QUARANTINE` | `<vault>/QUARANTINE` | damaged and refused arrivals |
| `MUSAEUS_RUNS_ROOT` | `<vault>/RUNS` | run logs and reports |
| `MUSAEUS_META_DIR` | `<vault>/MetaData` | your rule files |
| `MUSAEUS_LIBRARIES` | `<vault>/Libraries` | parent of the four trees below |
| `MUSAEUS_ALAC_ARCHIVE` | `<libraries>/ALAC-Archival` | the masters |
| `MUSAEUS_ALAC_LIBRARY` | `<libraries>/ALAC_Library` | lossless edition |
| `MUSAEUS_CAR_LIBRARY` | `<libraries>/CAR_Library` | car edition |
| `MUSAEUS_IPHONE_LIBRARY` | `<libraries>/iPHONE_Library` | iPhone edition |
| `MUSAEUS_PLAYLISTS` | `<libraries>/Playlists` | playlists |
| `MUSAEUS_BACKUP_ROOTS` | none | backup folders the bit-rot repair may copy from (`:`-separated) |
| `MUSAEUS_CURATOR_EXPORT_ROOT` | none | target of the older `curator` export |
| `MUSAEUS_DOC_ROOT` | `~/Documents/MUSAEUS` | the menu's document browser |
| `MUSAEUS_BUSY_TIMEOUT_MS` | `300000` (5 min) | how long SQLite waits on a lock |
| `MUSAEUS_NO_IDLE_THROTTLE` | unset | `1` stops long work pausing while you use the machine (the test suite sets it) |
| `MUSAEUS_NO_SLEEP_INHIBIT` | unset | `1` lets the machine go to sleep during a long run |
| `MUSAEUS_BATCH_FOLDERS` | off | `1` files each batch of masters under a dated folder |
| `MUSAEUS_AUTO_RESTART` | on | `0` keeps a running program going after its own source files changed (debugging) |
| `MUSAEUS_P0_SAFETY_GATE` | off | `1` is the same as `--safety-gate`: a stricter preflight that refuses a real vault (a rehearsal tool) |
| `MUSAEUS_CANONICALIZE_CHECKPOINT`, `MUSAEUS_FINALIZE_CHECKPOINT` | on | `0` runs that stage with no recovery copy and no journal. An escape hatch; leave it alone |

Set by the program for its own child processes, not by you:
`MUSAEUS_VERBOSE`, `MUSAEUS_PROGRESS`, `MUSAEUS_MASTERS_LOCK_FD`,
`MUSAEUS_MASTERS_LOCK_PARENT`, `MUSAEUS_SLEEP_INHIBITED`.

API keys, all optional. A stage without its key does nothing and says so:

| Key | Used for |
|---|---|
| `LASTFM_API_KEY` | genre for songs that have none |
| `ACOUSTICID_API_KEY` | naming untagged files and finding one recording under two encodings |
| `DISCOGS_CONSUMER_KEY`, `DISCOGS_CONSUMER_SECRET` | album and artist lookups MusicBrainz cannot answer |

MusicBrainz needs no key.

Rule files in `<vault>/MetaData/` are optional; without them the tags decide.
The main ones: `artist_canon.tsv` (one spelling per artist), `MasterLaw.csv`
(artist → genre), `Genre_Allowed.txt`, `Genre_Canonical_Map.txt`,
`artist_filing.tsv` / `person_folders.tsv` / `folder_names.tsv` (which folder
a song is filed in), `Composer_Canon.tsv`, `hold_keep.txt` and
`tribute_keep.tsv` (files the 30-day hold must never delete). The rules
behind them are in `docs/reconstruction/MUSAEUS_PROJECT_SCOPE.md`.

---

## Vault layout

```
<vault>/
├── INBOX/                       drop COPIES of new audio here
├── STAGING/                     working area during conversion
├── QUARANTINE/corrupted/        arrivals that do not decode
├── REVIEW/DUPES_MOVED/<date>/   duplicate copies set aside, with a manifest
│                                and a restore script
├── REVIEW/TRIBUTE_REMOVED/      karaoke and tribute copies set aside
├── RUNS/HANDOFFS/               a written report of each run: start here
├── RUNS/FAILURES/, LOGS/        detail for a failed stage; problem lists
├── MetaData/                    your rule files; TuneMyMusic.csv (wanted list)
├── Libraries/
│   ├── ALAC-Archival/<Genre>/<Artist>/<Album>/   the MASTERS
│   ├── ALAC_Library/            lossless edition, -18 LUFS, + Playlists/
│   ├── CAR_Library/             car edition, AAC, -14 LUFS, + Playlists/
│   ├── iPHONE_Library/          iPhone edition, AAC, -14 LUFS
│   └── ALAC_Library_Run_Logs/   a copy of each run's records
├── _db_backups/                 hash_index.db, editions.db, mb_cache.db
└── musaeus.db                   the catalogue (SQLite, WAL)
```

Mind the two spellings: `ALAC-Archival` (hyphen) holds the masters and
`ALAC_Library` (underscore) is an edition built from them.

---

## The pipeline

`musaeus run` runs four parts in order. `--act 1|2|3|enrichment` runs one
and stops, so you can read its problem list first. `--skip NAME` drops a
stage.

| Part | Stages | What happens |
|---|---|---|
| **Act 1** intake | preflight, ingest, permissions, sentinel, deny-list, scholar, acoustid-name, health, corrupt, albumart, normalize, spellcheck, sanitize, artist-consolidate, various-artists-fix, tribute-quarantine, genre-validate | files are registered, hashed by their decoded audio, read for tags; damaged files go to QUARANTINE; names and genres are settled |
| **Act 2** duplicates | cross-dupe, neardupe, dupe-resolver, classical-composer | copies of one recording are found; the keep rule picks one; the others move to REVIEW |
| **Act 3** library | canonicalize, finalize, bpm, forge, tagger, organize, audit | lossless → ALAC, lossy → AAC; the file becomes a master in `ALAC-Archival`; loudness is measured; tags and folders are set |
| **Enrichment** | enrich, mb_enrich, acousticid, mb-work, album-fill, title-complete, identity-tag, hold-expiry | genres, MusicBrainz ids, missing albums; last, the 30-day hold |

Stages skip what they have already done, so a second run is short. Each run
writes a report to `RUNS/HANDOFFS/` that names every problem and what it
means.

`--full`, `--archive`, `--maintain` and `--enrich` run older, shorter
chains; `musaeus run --help` lists them.

---

## Editions

Masters are never changed to suit a player. Each edition is a set of copies
built from them, and can be rebuilt.

```bash
musaeus edition-build lossless          # ALAC at -18 LUFS, + playlists
musaeus edition-build car               # AAC at -14 LUFS, noise bed under
musaeus edition-build iphone --budget-gb 60
musaeus edition-build car --dry-run     # what it would make
musaeus iphone-sync                     # copy the iPhone edition to the phone (VLC)
```

A build only makes what is missing or changed. It refuses to start while
other Musaeus work is running. `scripts/usb_transfer/transfer_to_usb.py`
copies the car edition to a USB stick.

---

## Duplicates, and what gets deleted

- **Exact** duplicates share decoded audio (a FLAC and an ALAC of one rip).
  **Near** duplicates share artist and title. **Acoustic** duplicates share
  a fingerprint.
- The **keep rule** picks the one copy to keep, in this order: studio over
  live; of two live copies the earlier; lossless over lossy; the original
  over a levelled copy, an arrangement or a remaster; the solo version over
  a guest version; then quality; then length. A copy you kept by hand is
  never replaced.
- Losers move to `REVIEW/DUPES_MOVED/<date>/` with a manifest and a restore
  script. `musaeus dedupe` lets you choose by hand instead.
- **After 30 days in REVIEW or QUARANTINE a file is deleted** (the
  `hold-expiry` stage, last in every run), unless its name is in
  `MetaData/hold_keep.txt`, its song is in `tribute_keep.tsv`, or the copy
  kept over it has left the library. Most deleted audio goes on a deny list,
  so the same download is refused if it arrives again (damaged files are
  not denied, so a good copy can still arrive).

---

## Checks and backups

```bash
musaeus doctor      # read-only: catalogue against disk against the ledgers
musaeus bitrot      # verify the masters against a stored baseline
musaeus deep-scan   # decode every master to find silent truncation
```

`scripts/music_backup.py` copies the masters to a backup drive and verifies
the copy. Nothing is scheduled for you: the maintainer runs `bitrot` and the
backup from systemd user timers.

**There is no rebuild.** `archive` is the primary record; the event log is
an audit trail and cannot reconstruct it (hashes were stored truncated, and
album, genre, year, track, duration and codec were never logged).
`rebuild-db` is disabled. `rebuild-from-disk` can recreate rows from the
files and their tags, but not your decisions. **Back up `musaeus.db` and
`_db_backups/`.**

---

## Commands

`musaeus --help` lists all of them; `musaeus <command> --help` has the
options. The ones used most:

| | |
|---|---|
| `setup`, `preflight`, `status`, `runs` | configure; check the machine; what is in the catalogue; past runs |
| `run`, `plan` | the pipeline; a read-only preview |
| `doctor`, `bitrot`, `deep-scan`, `health-report` | checks |
| `dedupe`, `possible-dupes` | choose duplicates by hand; list pairs that may be one song |
| `edition`, `edition-build`, `iphone-sync`, `playlist` | editions |
| `genre-suggest`, `genre-validate`, `canon-review` | genres and names |
| `report`, `upgrade-check`, `spec-scout` | reports |
| `reset` | wipe the database (you type RESET to confirm; files are not touched) |
| `ingest`, `sentinel`, `scholar`, `health`, `corrupt`, `albumart`, `normalize`, `spellcheck`, `sanitize`, `permissions`, `artist-consolidate`, `various-artists-fix`, `tribute-quarantine`, `cross-dupe`, `neardupe`, `dupe-resolver`, `canonicalize`, `finalize`, `bpm`, `forge`, `tagger`, `organize`, `audit`, `enrich`, `mb-enrich`, `acousticid` | one stage of the pipeline on its own |
| `ghost`, `integrity`, `auditor`, `original-year`, `convergence`, `db-tune`, `review`, `overnight` | occasional jobs: rows whose file is gone; a decode test; a loudness audit before Forge; first release years from MusicBrainz; did the last pass change less; tidy the database; approve albums and artists; an older nightly chain |
| `curator`, `transcode` | older exports, from before `edition-build` |
| `console`, `dry-run`, `version` | the menu; the same as `run --dry-run`; the version |
| `rebuild-from-disk`, `rebuild-db`, `review-report` | recreate rows from the files and their tags; disabled; not implemented |

Global options go before the command: `musaeus --no-progress run`.

---

## Database

SQLite in WAL mode at `<vault>/musaeus.db`. New columns are added when the
database is opened; there are no manual migrations.

| Table | Purpose |
|---|---|
| `archive` | one row per file: path, hashes, tags, status, loudness |
| `events` | append-only audit trail of every change |
| `duplicates` | duplicate groups and what was decided |
| `denied_hashes` | audio that was removed on purpose and must not return |
| `validation_issues` | problems the health check found |
| `fingerprints`, `mb_artist`, `mb_release`, `metadata_cache` | lookups kept so they are not asked twice |

`_db_backups/hash_index.db` remembers every filed recording across database
resets, and `_db_backups/editions.db` records every edition copy.

---

## Loudness

Forge measures each master with EBU R128 (`ffmpeg loudnorm`) and writes
ReplayGain tags; it does not change the audio. ReplayGain refers
to −18 LUFS and the R128 tag to −23 LUFS, so the two tags differ by 5 dB on
purpose. Editions are levelled copies: −18 LUFS lossless, −14 LUFS car and
iPhone, measured first and then applied (two passes).

---

## How it works

[docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) is the short tour: the layout
of the code, the life of one file, and the design decisions worth knowing
before you change anything. [CLAUDE.md](CLAUDE.md) lists the shared helpers
and the traps. [NOTES.md](NOTES.md) lists known limitations and what is
next. [CHANGELOG.md](CHANGELOG.md) is the history.

---

## Known limitations

- **`--dry-run` shows little on a first run.** About half the stages cannot
  preview ("no preview available"), because what they would do depends on
  hashes and tags that only a real run reads. The first honest preview is a
  real run on a small copy.
- **A base install fails two stages.** Without `rapidfuzz` and `essentia`
  the `neardupe` and `bpm` stages fail and `run` exits 1 (see "What it
  needs").
- **A wrong `MUSAEUS_VAULT_ROOT` makes a new, empty vault** instead of an
  error. Check `musaeus status` shows the vault you expect.
- **A failed preflight check does not stop the run.** Read its lines.
- **Linux only.** `iphone-sync` and the USB transfer need Linux tools.
- **One run at a time, one machine.** There is no server and no sharing.
- Two songs are left out of the maintainer's iPhone edition on purpose
  (encoder clicks at every setting).

---

## Development

```bash
pip install -e ".[dev,fuzzy]"
python3 -m pytest -q        # read the summary line, not the exit code
ruff check musaeus tests && ruff format --check musaeus tests && mypy musaeus
```

The suite needs `ffmpeg` and `fdkaac`; without them the tests that cover
conversion and deletion skip. It is about 5,200 tests and takes about ten
minutes.

---

## Repository

https://github.com/BogMan64/musaeus (public). MIT licence.

*Named for Musaeus of Athens — student of Orpheus, mythological keeper of
sacred songs.*
