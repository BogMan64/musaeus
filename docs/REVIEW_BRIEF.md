# MUSAEUS — review brief

**For the whole-program review of October 2026.** It replaces the brief of
2026-09-23, which covered one merge (PR #14).

**Repo:** https://github.com/BogMan64/musaeus

MUSAEUS is one person's music pipeline. It takes raw rips and downloads,
catalogues them, removes duplicates, names and tags them, files them as
masters in `Libraries/ALAC-Archival`, and builds three editions from the
masters: `ALAC_Library` (lossless, −18 LUFS), `CAR_Library` and
`iPHONE_Library` (AAC). It keeps the catalogue in SQLite, checks the masters
for bit rot monthly, and backs them up monthly. The owner has about 8,900
masters, with no other copy of many of them except the backups.

---

## The two slices

The review is split in two. Each slice is a review-only PR whose head is
byte-identical to `main`. Its base is `main` with only that slice's files
removed. So the diff is exactly the slice, and every other file is in the
tree at its current version: open any of them.

- **Slice 1: the pipeline and the catalogue.** `musaeus/` except the
  slice-2 modules: the CLI, the acts and stages (intake, duplicates, naming,
  genre, art, enrichment, finalize, organize), the keep rule, the database
  and its migrations.
- **Slice 2: everything that writes, copies or protects files.** The
  edition builds (`musaeus/edition_*.py`, `editions.py`, `loudness.py`), the
  safety layer (`musaeus/safety/`), bit-rot check and repair
  (`musaeus/stages/bitrot.py`, `musaeus/bitrot_repair.py`), the music
  backup, the USB and iPhone transfer, and the scripts that delete, swap or
  merge masters.

Each PR's description lists its files.

---

## The defect shape this codebase keeps producing

Not "bad code". **Two things that must agree, quietly ceasing to agree, while
both sides individually look correct, compile, import and pass lint.**

Found in the last month, each by checking the files rather than a report:

- The ReplayGain tag and the R128 tag, written from one measurement, 5 dB
  apart in every master (−23 vs −18 reference). Fixed in #73.
- `artist_canon.tsv` and the songs already filed: a rule applied only at
  intake, never to what was already in the library.
- A folder's `cover.jpg` and the songs in the folder: one picture shared by
  every artist in a mixed folder. Fixed in #72.
- The network gate refusing a lookup, and the lookup reporting "no cover
  found". A refusal read as an answer.
- The safety layer's checkpoint rooted at `STAGING` while the boundary was
  rooted at the vault, so the checkpoint was never consulted. Every test
  passed. Fixed in #82.

`ruff`, `pylint` and token-based clone detection find none of these. **A
reader checking for agreement is the only detector.**

---

## What to look for, in priority order

1. **Anything that can lose or damage a master.** The duplicate resolver,
   the delete tool (`scripts/delete_reviewed_tracks.py`), the swap tool,
   artist-folder merges, bit-rot repair (copies a backup over a master),
   backup rotation (deletes old backups), stick-sync deletions
   (`transfer_to_usb.py --sync`). For each: what has to be true before it
   deletes or overwrites, and is that actually checked, on the actual file?
2. **Two things that must agree.** The catalogue (`archive` table) against
   the files on disk against the edition ledger (`editions.db`); the rule
   files (`artist_canon.tsv`, genre rulings) against songs already filed;
   ReplayGain against R128; an edition copy against its master.
3. **Checks that report OK having checked nothing.** `verify_effect`
   returning `[]` ("looked, found nothing wrong") when it looked at nothing;
   it must return `NO_VERIFICATION` instead. Swallowed exceptions. A refused
   or failed network lookup counted as "no result".
4. **Interruption.** A power cut or kill in the middle of a build, a
   delete, a swap, a backup or a repair. Is a re-run safe? Does anything
   half-written look finished? (Write to a temp name, verify, then rename.)
5. **Timing.** The monthly timers (`musaeus-bitrot.timer` on the 15th at
   04:00, `musaeus-music-backup.timer` on the 1st at 03:30) starting while a
   run or an edition build is active. What guards that, and is the guard
   checked by the thing that acts?
6. **The safety layer** (`musaeus/safety/`), after #82. It guards every file
   finalize and canonicalize move.
7. **Secrets.** No keys in the repository, the logs or the reports.
8. **Tests that pass for the wrong reason.** A test that would still pass
   with the fix reverted; a fixture that cannot reach the code path its name
   claims (fake bytes where the bug needs real tags); a mock that answers
   the question the test was meant to ask.

---

## Known and accepted — do not re-report

- **`sha256_file` (`musaeus/safety/manifest.py`) duplicates
  `hasher.file_hash`.** A nit, recorded as F5 in the September review.
- **Canonicalize's INBOX originals get no precondition digest check.** They
  are outside its STAGING checkpoint; its `_open_boundary` docstring says so
  ("WEAKER THAN FINALIZE'S, DELIBERATELY"). Rollback does restore them.
- **A tag-captured checkpoint entry cannot see a byte edit that keeps size,
  mtime and tags.** Documented in `build_manifest`.
- **Edition tests refuse to run while a musaeus process is running.** The
  build guard does that on purpose; about 8 edition tests fail locally if a
  run or build is active. They pass on CI and on an idle machine.
- **The 3.11 CI job occasionally hangs** and passes on re-run.
- **Data, not code:** two songs are left out of the iPhone edition (encoder
  clicks at every bandwidth; the owner chose to leave them); about 200 songs
  have no album.
- **`scripts/car_library/` and `scripts/alac_library/` are retired**
  standalone builders, replaced by `musaeus edition-build`. The one-off
  repair scripts in `scripts/` are outside both slices.
- **`UP031` printf-format warnings in `scripts/`.** CI lints only
  `musaeus/` and `tests/`. Converting them is a trap (below). Leave them.
- **Duplicated judgement is intentional.** Sharing the mechanism is right;
  sharing the judgement usually is not. `neardupe` takes the bracket
  alphabet but keeps its own rule about which annotations are safe to strip,
  because "Here I Am (Come and Take Me)" must not collapse to "Here I Am".
- **Fixed since September, verified by test: do not re-report.** Resolver
  R1-R5 (#81, and R2/R3 earlier); safety A, B, F1-F4, F6 (#82). The
  findings table is in `docs/reconstruction/MUSAEUS_TODO.md`.

---

## Traps that compile, import, lint and lie

Every one was hit for real in this repo. If the review proposes a change near
any of these, it must say which one it is avoiding.

- An f-string eats a regex quantifier: `\d{2}` becomes `\d2`. Both compile.
- `ffmpeg` exits 0 on a truncated file; it says so on stderr. Check
  `returncode == 0 AND not stderr`.
- `ffmpeg` reads stdin and eats the enclosing loop's input. Every `ffmpeg`
  call inside a `while read` loop needs `-nostdin`.
- Single-pass `loudnorm` is not the two-pass bake.
- Metadata cannot see truncation: an MP4's durations live in the `moov`
  atom, written before the audio. Only a decode knows.
- An unstated format property is inherited from the input (sample rate,
  channels, bit depth). This has shipped four bugs.
- Existence is not completeness. Write to `.part`, verify, then rename.
- `pgrep -f` matches the shell that is asking. Use
  `scripts/musaeus_running.sh`.
- The network policy defaults to refusing. A script that forgets to allow
  it gets "nothing found" from every lookup, which reads exactly like a
  real empty answer.

---

## How to report

For each finding: the two things that disagree, the file:line of each, and a
concrete failure: inputs or state, and the wrong output or silent no-op that
results. A finding with no stated failure path is not a finding.

Say plainly which findings were verified by running something and which were
read-only inferences. Do not mark anything verified that was not observed in
a tool result. Nine unverified guesses are worse than two measured facts.

Test command: `python3 -m pytest -q` (the suite sets
`MUSAEUS_NO_IDLE_THROTTLE=1`; without it the idle throttle SIGSTOPs ffmpeg
children and tests fail by timing out, which reads exactly like a slow
disk). Baseline on `main` at `59b0ccb`: **3,781 passed, 13 skipped** locally; CI
runs 3.10, 3.11 and 3.12. Read the summary line, not the shell exit code.
