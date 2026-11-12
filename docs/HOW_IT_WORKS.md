# How Musaeus works

A short tour for whoever opens this in six months. Written 2026-11-11
against `main` at `3ff57d7`. For rules and rulings read
`docs/reconstruction/MUSAEUS_PROJECT_SCOPE.md`; for the traps read
`CLAUDE.md`; for what to look for in a review read `docs/REVIEW_BRIEF.md`.

## The idea in five lines

1. Every file gets one row in `archive` (SQLite). The row is the truth
   about the file; the event log says why each change happened.
2. A file is identified by the hash of its **decoded audio**, so a FLAC and
   an ALAC of one rip are the same recording, and retagging changes nothing.
3. One copy of each song is kept, chosen by one rule (`keep_rule.py`).
4. Kept files become **masters** in `Libraries/ALAC-Archival`. Masters are
   never edited in place and never levelled.
5. Everything you play is an **edition**: copies built from the masters,
   recorded in a ledger, and rebuildable.

## The life of one file

```
INBOX/song.flac
  Act 1  ingest        a row, status PENDING
         sentinel      audio hash; an exact twin is marked a duplicate
         scholar       tags read with ffprobe; status CATALOGUED
         corrupt       decoded once; a damaged file goes to QUARANTINE/corrupted
         normalize ... names, artist spelling, genre settled from your rule files
  Act 2  neardupe      same artist and title as a song already held?
         dupe-resolver the keep rule picks one; losers move to REVIEW/DUPES_MOVED
  Act 3  canonicalize  FLAC -> ALAC (lossy -> AAC), written to STAGING, verified
         finalize      moved to Libraries/ALAC-Archival/<Genre>/<Artist>/<Album>/
         forge         loudness measured; ReplayGain tags written
         tagger        the row's tags written to the file
         organize      folder and file name follow the tags
         audit         catalogue, disk and hash index must agree
  Enrichment           MusicBrainz ids, missing album names, fingerprints
         hold-expiry   deletes what REVIEW and QUARANTINE have held 30 days
```

The order is `CANONICAL_PIPELINE` in `musaeus/stages/__init__.py`; the
comments there say why each stage sits where it does.

## Where things are

| Part | Files | What it holds |
|---|---|---|
| Entry | `cli.py`, `console.py` | every command; the menu |
| Run | `context.py`, `stages/base.py`, `planner.py` | one `RunContext` per run; the stage contract; the dry-run plan |
| Stages | `stages/*.py` | one file per stage; each has `run`, `dry_run`, `validate`, `verify_effect` |
| Catalogue | `db.py` | schema, opening the database, adding columns |
| Identity | `hasher.py`, `duration.py`, `recording_identity.py` | audio hash; how long a file is; is this file that recording |
| Names | `artist_form.py`, `brackets.py`, `title_case.py`, `filing.py`, `canon/` | one spelling, one folder, one genre per artist |
| Duplicates | `keep_rule.py`, `stages/dupe_resolver.py`, `stages/neardupe.py`, `stages/cross_dupe.py`, `stages/acousticid.py`, `stages/mb_work.py`, `dedupe.py` | finding copies, choosing one |
| Writing files | `safe_save.py`, `masters_lock.py`, `file_lock.py` | change a copy beside the file, then rename; one lock on the masters |
| Safety | `safety/` | checkpoint, journal and quarantine around canonicalize and finalize |
| Editions | `editions.py`, `edition_build.py`, `edition_bake.py`, `edition_ledger.py`, `iphone_sync.py` | which songs; the build; the loudness bake; the record of copies |
| Protection | `stages/bitrot.py`, `bitrot_repair.py`, `music_backup.py`, `deep_scan.py`, `doctor.py` | bit rot, backup, decode check, the integrity report |
| Network | `network_policy.py`, `stages/mb_enrich.py`, `discogs.py`, `art_sources.py` | every lookup goes through one gate that refuses by default |
| Reports | `handoff.py`, `run_records.py` | the per-run document; run logs |
| Tools | `scripts/` | one-off repairs and reports. Four are loaded by the CLI |

## Decisions worth knowing before you change anything

- **`archive` is primary; there is no rebuild.** The event log cannot
  reconstruct it. `rebuild.py` is disabled on purpose. Back up the database.
- **Masters are never saved in place.** Forge, Tagger, BPM, IdentityTag and
  AlbumArt write a copy beside the master and rename it over
  (`safe_save.change_beside`). A power cut leaves the old file or the new
  one, never half of each.
- **Write to a temporary name, verify, then rename.** A half-written file
  looks finished and is skipped for ever.
- **A stage must say whether it looked.** `verify_effect` returns
  `NO_VERIFICATION` for "I did not look" and `[]` for "I looked and found
  nothing wrong". Mixing them once let a stage report success while every
  write failed.
- **Removals are moves, with a clock.** Duplicates and tribute copies go to
  `REVIEW/`, damaged and refused arrivals to `QUARANTINE/`. After 30 days
  `hold-expiry` deletes them and denies their audio. Outside that hold, no
  master is deleted without a person's yes.
- **Deleted audio is denied.** `denied_hashes` and the deny-list stage stop
  a removed recording from coming back with the next download.
- **`hash_index.db` outlives the catalogue.** It lives in `_db_backups/`
  and remembers every filed recording. The rule: anything expensive to
  compute and cheap to lose belongs there, not in `musaeus.db`.
- **Share the mechanism, not the judgement.** Small helpers exist once
  (`CLAUDE.md` lists them) and a test or a semgrep rule guards each. Rules
  about what a duplicate is stay in the stage that owns them.
- **The network refuses by default.** A script that forgets to allow it gets
  "nothing found", which reads like a real empty answer. A failed lookup is
  never recorded as "not found".
- **ffmpeg lies politely.** It exits 0 on a truncated file and inherits any
  format property you do not state. Check stderr; state sample rate,
  channels and bit depth; measure the output file, not the report.
- **One run at a time.** `masters_lock.py` and the process check in
  `edition_build.py` enforce it. Do not run a `musaeus` command while the
  test suite runs: the busy guard fails tests.

## Known dead weight

Recorded in `docs/REVIEW_BRIEF.md` as known and accepted: most of `state/`
(migrator, projector, cancellation, `DuplicateRepository`), `scheduling.py`
and `reporting.py` have tests but no caller in the program a user runs.
`--full`, `--archive` and `--maintain` are older, shorter pipelines kept
from before the Acts. `scripts/car_library/` and `scripts/alac_library/` are
retired builders replaced by `musaeus edition-build`.

## How work is done here

One pull request per change. A fix comes with a test that fails on the old
code. `ruff check`, `ruff format --check` and `mypy` before a push. The full
suite (about 5,200 tests, ten minutes, needs `ffmpeg` and `fdkaac`) before a
merge. CodeRabbit reviews each pull request. Rulings by the owner are dated
in the code comments next to what they changed.
