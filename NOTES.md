# Notes for when you come back

Written 2026-11-11, at the end of a wrap-up check of `main` at `3ff57d7`.
Read this first; then `docs/HOW_IT_WORKS.md`; then the handover file kept
outside the repository for the day-to-day state.

How the check was done: the full suite on `main` (5,233 passed, 15
skipped); a fresh clone installed in a clean home folder; `musaeus setup`,
then `run`, `doctor` and the lossless and car builds on a scratch vault
with 13 test files (7 real songs, a FLAC twin, an MP3 copy, an untagged
WAV, a cut file, an empty file, a text file named `.mp3`). The real vault
was only read.

## What works, seen in that run

- Exact duplicates were found across formats (FLAC against ALAC, WAV
  against ALAC) and set aside with a manifest and a restore script.
- The three damaged files went to `QUARANTINE/corrupted`; the report said
  in plain words what was wrong and what to do.
- 13 files took 2 minutes the first time, 15 seconds the second.
- The lossless and car editions built and every copy decoded clean.
- `doctor` agreed with the disk.

## Tests on 2026-11-11

- Branch `chore/wrap-up-20261111`: **5,347 passed, 15 skipped** (the 5,233
  of `main`, 7 new, and the 107 lab tests the local runner used to skip).
  `ruff`, `ruff format`, `mypy` and the semgrep rules are clean.
- Coverage of `musaeus/`, measured in the same run: **83%**. The modules
  that move or delete masters (canonicalize, finalize, the resolver, the
  hold, organize, bit rot, backup, the safety layer) are at 86–100%.
- The thin spots are older on-demand stages nobody uses daily:
  `stages/integrity.py` 25%, `stages/auditor.py` 28%, `stages/transcode.py`
  40%, `stages/curator.py` 60%. Decide to test them or retire them.
  `cli.py` (55%) and `console.py` (59%) read low because tests that start
  the program as a separate process are not counted.

## Known limitations

1. **A base install fails two stages on every run.** Without `rapidfuzz`
   the `neardupe` stage fails; without `essentia` the `bpm` stage fails.
   The run goes on, ends "finished with errors" and exits 1. In the test,
   an MP3 copy of a song already held was filed as a second master because
   `neardupe` did not run. The Debian package depends on neither.
2. **The run report calls a missing package "a crash ... needs a code
   fix".** The error line itself says `pip install rapidfuzz`. The same for
   a folder that cannot be written: "not a known kind of problem".
3. **A failed preflight check does not stop the run.** With INBOX read-only
   the line read "preflight: OK ... errors=1" and the run went on to fail
   in the duplicate resolver. `run --act 1` then exited 0.
4. **A wrong `MUSAEUS_VAULT_ROOT` makes a new empty vault.** `musaeus
   status` on a path that does not exist creates the folder and a
   database, and reports an empty library. A mistyped path, or a drive
   that is not mounted, looks like a library with nothing in it.
5. **`--dry-run` shows little on a first run**: 20 of 36 stages have no
   preview.
6. **MusicBrainz lookups are slow**: 55 seconds for 8 songs, with a
   "rate-limited, backing off 5s" after almost every artist. The requests
   carry a made-up contact address (`contact@musaeus.local`); MusicBrainz
   slows clients that do not say who they are. Not measured further.
7. **The first line a new user sees is a warning about damaged audio.** It
   is the ffmpeg self-test reading its own cut file. It appears once per
   ffmpeg version.
8. **Hold-expiry cannot close a held row whose file is gone.** `doctor`
   now warns about such rows; nothing closes them. (The 1,955 of
   2026-11-11 were closed by hand with a script.)
9. **`iphone-sync` reads the phone's free space once**, before a build
   that can take hours.
10. **The Docker image is from September** and has no `fdkaac`, so it
    cannot build the car and iPhone editions. It has not been rebuilt or
    tested since.
11. **Linux only.** The Windows port is paused with two Windows tests
    failing on the base.
12. **No GitHub release exists.** The `.deb` builds from a checkout only.

13. **The original of a converted file is kept for ten runs, not for a
    time.** `RUNS/recovery/` keeps the newest ten folders of each kind
    (`run_records.py`, `KEEP = 10`). After ten more runs that convert
    something, the FLAC behind an ALAC master is gone, however soon.

## Shortcuts taken

- **Four commands load code from `scripts/`** (`report`, `upgrade-check`,
  `spec-scout`, `canon-review`). They now find it from any folder, but a
  plain `pip install .` still does not ship it. Moving the four helpers
  into the package would end this for good.
- **About 60 functions have no caller in the program** (`vulture`, 60%
  confidence), most in `state/`, `scheduling.py` and `reporting.py`. Known
  and accepted in `docs/REVIEW_BRIEF.md`; tests keep them alive.
- **`--full`, `--archive` and `--maintain`** run pipelines from before the
  Acts. `--full` is shorter than the default, despite its name.
- **`musaeus review-report` is listed and does nothing.** There is no
  reviewer stage; the command prints the help and exits 1. `rebuild-db` is
  listed and disabled on purpose.
- **`scripts/` holds about 30 one-off repair scripts** with this machine's
  paths written into them. CI does not lint them (27 small lint findings,
  left alone: untested code, no gain).
- **The local suite runner skips 107 tests.** `suite_chunks.sh` runs
  `tests/test_*.py` only; the labs under `scripts/fm_radio/tests` and
  `scripts/discography/tests` run in CI and by plain `pytest`. They pass.
- **CI runs the whole suite twice per job**: the "Report skipped tests"
  step calls `pytest` again to print one line.
- **Dependencies are not pinned.** `mutagen>=1.46` is the only required
  one; there is no lock file. The versions in use on 2026-11-11 (mutagen
  1.46.0 and 1.48.1, rapidfuzz 3.14.5, rich 13.9.4, psutil 7.2.2, openpyxl
  3.1.5) have no known vulnerability (`pip-audit`).
- **The version number has never moved.** `0.1.0` since July 2026.

## Housekeeping left

- 310 branches on GitHub, 269 of them already merged; 206 local branches.
- Five review-only pull requests are still open (#143–#147). Their
  findings were all fixed in October.
- `docs/reconstruction/MUSAEUS_TODO.md` and `MUSAEUS_PRIORITIES_*.md` stop
  at 2026-10-11. A month of work (#177–#320) is only in the handover file.
- The scratch folder beside the repository holds about 66 GB (a test image of the
  car stick is most of it).
- The repository is public; the README said "private" until this check.

## Ideas for next steps

In the order I would take them:

1. Make `rapidfuzz` a real dependency, and let a missing `essentia` skip
   `bpm` with a note instead of failing the run.
2. Teach the run report two more kinds of problem: "a Python package is
   missing" and "a folder cannot be written".
3. Stop the run when a preflight check fails.
4. Refuse a vault root that does not exist, except in `musaeus setup`.
5. Close held rows whose file is gone (needs a yes: it deletes rows).
6. Tag a release and publish the `.deb`, or take the Releases link out of
   the docs for good.
7. Bring `MUSAEUS_TODO.md` up to date from the handover file, or retire it.
8. Delete the merged branches and close #143–#147.
9. From the wishlist: a pre-wipe rehearsal command; a "what lives where"
   audit in `doctor`.

## Things that will bite you

- Do not run a `musaeus` command while the test suite runs. The busy guard
  fails tests.
- The installed program is the main checkout (an editable install). Editing
  that folder changes the program you run. Work in a worktree.
- Tests must keep their temporary folder under `/tmp`: the safety layer
  treats any other path as real data.
- `musaeus.db` cannot be rebuilt from the event log. Back it up.
- A rule file in `MetaData/` acts at intake. Songs already filed do not
  follow a changed rule until a run or a script applies it.
