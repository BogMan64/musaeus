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

## The slices

A review accepts at most 8,000 changed lines, so the program is reviewed in
slices. Each slice is a review-only PR whose head has `main`'s tree (plus
this brief, when it changed after the slice was cut). Its base is that tree
with only the slice's files removed. So the diff is exactly the slice, and
every other file is in the tree at its current version: open any of them.

The three slices that can lose or damage a master come first:

- **#86, the masters:** the duplicate finders and resolver, the keep rule,
  canonicalize, finalize, organize, transcode, deny list, tribute quarantine.
- **#87, protection:** the safety layer, the bit-rot check and its repair,
  the music backup, and the tools that delete, swap or merge masters.
- **#88, the editions:** the edition builds and ledger, loudness, and the
  USB and iPhone transfer.

**Not in any open review yet: about 32,800 lines**, by the owner's choice
until the first three have been read. That is most of `musaeus/`: `cli.py`,
`db.py`, `state/` and its migrations, `canon/`, `network_policy.py`,
`art_sources.py`, `stages/albumart.py`, `stages/forge.py`, `doctor.py`, the
intake stages and the enrichment stages. Targets 2 and 3 below point at that
code too; a finding there is welcome, but nothing there has been read whole.

Each PR's description lists its files. This brief is the one copy of the
targets and the known-and-accepted list; the PR descriptions point here.

---

## The defect shape this codebase keeps producing

Not "bad code". **Two things that must agree, quietly ceasing to agree, while
both sides individually look correct, compile, import and pass lint.**

Found in the last month, each by checking the files rather than a report:

- The ReplayGain tag and the R128 tag, written from one measurement, were
  EQUAL in every master. They must differ by 5 dB, because ReplayGain
  refers to −18 LUFS and R128 to −23: every master played 5 dB too quiet in
  a ReplayGain player. Fixed in #73: `forge.py` now writes RG = R128 + 5 dB.
  **Being 5 dB apart is correct; do not "fix" it.**
- `artist_canon.tsv` and the songs already filed: a rule applied only at
  intake, never to what was already in the library. Known: the file acts in
  Act 1 only, and the owner's rulings are applied to filed songs by hand
  with `scripts/consolidate_artist_folders.py`.
- A folder's `cover.jpg` and the songs in the folder: one picture shared by
  every artist in a mixed folder. Fixed in #72.
- The network gate refusing a lookup, and the lookup reporting "no cover
  found". A refusal read as an answer. **Still open in one place**:
  `stages/albumart.py` marks a song checked before fetching its cover
  (:476-479), and the next run selects only unchecked songs (:433), so a
  refused or timed-out lookup leaves the song without a cover for good
  unless someone passes `--force`.
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
6. **The safety layer** (`musaeus/safety/`), after #82. It guards the files
   finalize and canonicalize move -- when it can be opened. If the recovery
   checkpoint cannot be created the boundary is "UNAVAILABLE" and both
   stages carry on without it: canonicalize then deletes INBOX originals
   with no quarantine copy (`canonicalize.py:1013`) and finalize copies with
   no journal (`finalize.py:652`). Known and open (see the TODO table).
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
  ("WEAKER THAN FINALIZE'S, DELIBERATELY"). With the boundary open, rollback
  restores them (#82); without it, see target 6.
- **A tag-captured checkpoint entry cannot see a byte edit that keeps size,
  mtime and tags.** Documented in `build_manifest`.
- **The edition build refuses to start while other MUSAEUS work runs.**
  That guard is deliberate. Tests used to read the real process table
  through it, so they failed whenever MUSAEUS was running on the test
  machine; an autouse fixture now stubs `edition_build.pipeline_pids` for
  in-process tests. Tests that run the build as a subprocess still read the
  real table: with a stand-in `musaeus run` alive, 3 edition tests fail
  (6 before the fixture), and `test_a_killed_build_leaves_nothing_half_made`
  can fail the same way.
- **Much of the P0 layer has no live caller.** `migrate()`,
  `DuplicateRepository`, `run_state`, `scheduling` and `reporting` are not
  wired to the program a user runs, and the `state/migrations` chain never
  runs (only the migrations in `db.py` do). Known; confirm, don't rediscover.
- **The 3.11 CI job occasionally hangs** and passes on re-run.
- **Data, not code:** two songs are left out of the iPhone edition (encoder
  clicks at every bandwidth; the owner chose to leave them); about 200 songs
  have no album.
- **`scripts/car_library/` and `scripts/alac_library/` are retired**
  standalone builders, replaced by `musaeus edition-build`. The one-off
  repair scripts in `scripts/` are outside the review slices.
- **`UP031` printf-format warnings in `scripts/`.** CI runs `ruff` only on
  `musaeus/` and `tests/` (semgrep, with `--error`, covers `scripts/` too).
  Converting them is a trap (below). Leave them.
- **Duplicated judgement is intentional.** Sharing the mechanism is right;
  sharing the judgement usually is not. `neardupe` takes the bracket
  alphabet but keeps its own rule about which annotations are safe to strip,
  because "Here I Am (Come and Take Me)" must not collapse to "Here I Am".
  Each stage declaring its own columns is deliberate too.
- **Fixed since September, verified by test: do not re-report.** Resolver
  R1-R5 (#81, and R2/R3 earlier); safety A, B, F1-F4, F6 and the move-undo
  fix (#82).
- **Found by the October reviews of #86 and #87:** tracked, with status, in
  the table in `docs/reconstruction/MUSAEUS_TODO.md`. Do not re-report those.

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
- A docstring is an `ast.Constant`: an AST guard that greps string
  constants flags prose that merely discusses the thing.
- Repair loudness through the bake's own two steps (`ffmpeg_measure_loudnorm`,
  then `build_second_pass_filter`), never through a command that looks
  equivalent.
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
