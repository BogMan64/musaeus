# MUSAEUS — TODO

**Merged 2026-09-07.** This file was two: MUSAEUS_TODO.md ("execution order,
defers to OPEN_ITEMS for what is open") and MUSAEUS_OPEN_ITEMS.md ("source of
truth on what is open, execution order lives in TODO"). Each named the other
as authoritative, so neither was, and both had drifted — OPEN_ITEMS still
listed the no-genre rows and the advisory deny entries as open when both were
closed. That is the same two-places-disagreeing failure that produced every
bug found this week. One file now. MUSAEUS_OPEN_ITEMS.md is archived.

Every count below was verified against the live vault on 2026-09-07, not
carried forward.

**Claude Opus 5 access ends 2026-09-08. Kiro runs ~30 days after.**

**Wants live in `MUSAEUS_WISHLIST.md`, not here** (split 2026-09-07). The
line is: a TODO item has a cost if left undone — something is unprotected or
a check is lying. A wishlist item has none; the library is correct without
it. Given limited time, take the TODO.

---

## P0 — running now, no action needed

**The LUFS bake** (`scripts/alac_library/build_alac_library.py --execute`),
PID 951539. **~1,800 of 16,200 left.**

It only runs while you are away from the keyboard — IdleThrottle SIGSTOPs the
encoder and releases 40s after the last input, so `ps` showing state `T` is
correct behaviour, not a stall. Measured: **325/h unattended, 0/h during an
hour of conversation.**

**Queued behind it:** the decode scan of all files never decode-checked
(PID 1953006, waits on the bake, writes to
`~/Desktop/MUSAEUS_corrupt_scan_2026-09-07.log`). This is the highest-value
thing still to run — four corrupt masters surfaced overnight that only
decoding could see, and ~74% of the library has never been checked.

Do NOT run `organize` while the bake runs; both move files.

---

## P1 — needs me, before Sept 8

1. ~~**Lossless-fraud detection.**~~ **BUILT, MEASURED, REJECTED 2026-09-07.**
   It does not work on this library, and the data says so plainly. Do not
   rebuild it without reading this first.

   Method: decode 25s from mid-track, 8192-point FFT, find the highest
   frequency still within 45 dB of the musical band. A lossy encoder should
   leave a brick wall — 16 kHz at 128 kbps, ~19 kHz at 320.

   Calibrated against 20 files whose SOURCE codec was AAC (certainly lossy)
   and 20 whose source was FLAC (certainly lossless):

       lossy    14.6 16.0 17.2 17.2 17.3 17.4 18.4 19.0 19.1 19.6
                19.9 20.0 20.1 20.3 20.6 20.7 20.8 20.9 20.9 21.1   median 19.8
       lossless  9.9 14.4 15.5 15.6 16.0 16.9 18.8 19.4 20.3 20.6
                20.7 20.9 21.1 21.1 21.4 21.6 21.7 22.1 22.1 22.1   median 20.6

   **80% of files fall in the overlap band.** Every threshold mislabels at
   least as many genuine lossless files as it catches lossy ones:

       <16 kHz  catches  1/20 lossy, mislabels 5/20 lossless
       <17 kHz  catches  2/20 lossy, mislabels 6/20 lossless
       <18 kHz  catches  6/20 lossy, mislabels 6/20 lossless
       <20 kHz  catches 12/20 lossy, mislabels 8/20 lossless

   Two reasons, both specific to THIS library. Modern AAC at 256 kbps keeps
   content to 20–21 kHz, so it is spectrally indistinguishable from lossless.
   And genuine lossless transfers of 1950s–70s recordings are band-limited by
   the master tape, not the encoder — The Who's "The Kids Are Alright" tops
   out at 14.7 kHz and one FLAC reaches only 9.9 kHz. A library weighted to
   that era is the worst possible case for this test.

   Shipping it would flag Grey's oldest and most valuable recordings as
   frauds. That is the crying-wolf failure, with the added cost that a
   "this master is fake" report invites deleting a good file.

   The probe is kept at `~/.claude/jobs/39e88957/tmp/fft_probe.py` if anyone
   wants to re-measure. Doing it properly needs encoder-artefact analysis
   (scale-factor banding), not a cutoff threshold.

2. ~~**Push the pending commits.**~~ **DONE 2026-09-08.** Eight commits
   pushed to `fix/dedupe-policy-and-permissions-sweep` (`0394814..1ab15f2`),
   including the decode gate and the cover-art fix. Nothing unpushed.

## P1a — added 2026-09-08 (decode gate + bit rot)

1. ~~**`bitrot` is protecting 8.8% of the masters.**~~ **It was protecting
   0%.** Measured 2026-09-08:
   `archive_tier_hashes` holds **1,385 baselines** against **15,816 files**
   in `ALAC_Archive`. The stage works — it is the only thing in MUSAEUS that
   can catch a file which decoded cleanly in September and rots in January —
   but a file with no baseline entry is reported as *new*, not as corrupt,
   so 91% of the archive is currently outside its reach.

   **Do this:** `~/musaeus_jobs/bitrot.sh baseline`, once, after the CAR
   build finishes. Then `~/musaeus_jobs/bitrot.sh verify` on whatever
   schedule you like.

   **Read before re-baselining:** a rebaseline records whatever is on disk
   *right now* as the truth. Baseline a file that is already rotted and the
   rot becomes the baseline and is never reported again. Baseline after a
   bake, never in response to an unexplained mismatch.

   **DONE 2026-09-08** — and a verify pass first showed the 8.8% figure was
   generous. `archive_tier_hashes` keys on **path**, and every one of the
   1,385 baselined paths was gone from disk:

       files to verify: 15816
       ok: 0
       corrupt (hash mismatch): 0
       new (no baseline yet): 15816
       missing from disk (was baselined, gone now): 1385

   So the baseline was not 8.8% valid. It was **0%** valid. Nothing on disk
   had a usable baseline, and the rebaseline had nothing to overwrite —
   which is the only reason it was safe to run against a live library.

2. ~~**`bitrot`'s baseline goes stale every time a file moves.**~~
   **FIXED IN CODE 2026-09-08 — but the live vault still needs the
   migration below.** Kept in full because the reasoning is what stops it
   being undone.

   **The four commands, in this order. Order matters.**

   ```
   ~/musaeus_jobs/bitrot.sh baseline    # running since 21:12, ~5 h
   ~/musaeus_jobs/bitrot.sh backfill    # ~3 h — gives each row its PCM identity
   ~/musaeus_jobs/prune_stale_baselines.sh
   ~/musaeus_jobs/bitrot.sh verify      # should now say ok: ~15,816, new: 0
   ```

   The baseline currently running was launched **before** the fix, so its
   rows carry byte hashes and no PCM identity. `backfill` adds the identity
   without recomputing a single SHA-256 — on this vault that is 592 GB of
   work not repeated. Run it before the prune: the prune's safety rule
   depends on it.

   **Why the prune rule is narrower than "the path is gone".** A file that
   MOVED also has a baseline row whose path is gone — and that row is
   precisely what lets verify recognise the move. Deleting by missing path
   alone would throw those away and quietly restore the bug. The script
   deletes only rows that are **both** pathless **and** without a PCM
   identity, i.e. rows nothing can ever match again.

   The original diagnosis follows.

   ~~This is the real defect the verify pass exposed, and it will silently
   recur.~~

   `archive_tier_hashes.path` is the key. `organize`, `canonicalize`,
   `finalize` and `migrate_to_archive` all rename or relocate files as a
   matter of course, and the LUFS bake rewrites `archive.file_path`
   outright. Every one of those quietly orphans the baseline row. A verify
   run afterwards reports the file as **new**, not as corrupt — so the check
   goes silent rather than loud, and a silent integrity check reads exactly
   like a passing one.

   That is the same shape as the `library files with no row: 0` incident:
   a green result that means "I looked at nothing".

   **Options as they stood before the fix** (the first was taken):
   - key the baseline on `audio_hash` (PCM identity, already computed,
     already survives re-tagging and moves) instead of on path
   - failing that, re-baseline as the last step of any run that moves
     files, and make `verify` FAIL rather than pass when the "new" count
     is a large fraction of the library

   Until one of those lands, treat a clean `bitrot verify` as meaningful
   only if its `new:` count is near zero. **Check that line first.**

3. **`--dry-run` previews nothing for 21 of the 31 stages.** It prints
   `no preview available for this stage` and the run wrapper never calls
   the stage at all (`musaeus/planner.py:199` — a stage without a
   `plan_candidates` method gets a `None` count and is skipped).

   Affected: `preflight, permissions, sentinel, deny-list, scholar, health,
   corrupt, albumart, artist-consolidate, various-artists-fix,
   tribute-quarantine, cross-dupe, neardupe, classical-composer,
   canonicalize, finalize, organize, enrich, mb_enrich, identity-tag,
   bitrot`.

   This has already cost real work: an overnight `musaeus corrupt
   --dry-run` scan was queued and did nothing, returning in zero seconds.
   The output is not wrong — it says plainly that it cannot preview — but
   it is easy to read as "nothing to do", especially at the end of a long
   day.

   **Do not use `--dry-run` to estimate the scope of those stages.** It is
   not a preview; it is an admission that there is no preview.

   **This is NOT fixed by making the CLI call each stage's own `dry_run()`
   method.** Those methods exist and look like the obvious answer; calling
   them from the CLI would undo P0-02. `--dry-run` routes to the planner
   deliberately, precisely so it no longer means "execute with a flag set":
   the planner never instantiates a stage, never opens a writable
   connection and never calls `ensure_dirs()`. That guard is the reason the
   blanket refusal on `--dry-run` could be lifted at all.

   **The correct fix is to give a stage a `plan_candidates(conn, cfg)`
   method** — a pure, read-only count against a read-only connection, which
   is what the planner already calls where one exists. Ten stages have one.
   Worth doing for `corrupt` and `bitrot` first, since those are the two
   whose scope somebody actually wants to estimate before committing hours
   to a run.

4. **Run `~/musaeus_jobs/recheck_decode_failures.sh` once, after the
   2026-09-08 sweep finishes.** That sweep was launched from code that
   counted any ffmpeg stderr as damage, and kept it in memory for the whole
   run. Broken cover art therefore reads as broken audio at a rate of about
   0.15% — three false accusations in the first 1,950 files, so expect
   roughly 18 by the end. The recheck decodes only the rows already marked
   damaged and clears the ones that were never damaged. Real damage stays
   flagged.

   **The CSV that sweep writes to the Desktop is not corrected by the
   recheck** — it is a file, written once, and it will list those false
   accusations. Read the recheck's own output as the answer, not
   `MUSAEUS_decode_failures_2026-09-08.csv`. Nothing was moved or deleted
   either way; a wrong entry there costs a second look, not a file.

5. **`Spirit of the West — Homelands [Jigs - the Kesh, the Blackthorn
   Stick]` is damaged. Re-source it.** Found by the new pre-bake gate on its
   first run, so it is one of the four previously-undiagnosed unbaked rows.

   Diagnosed 2026-09-08. The master in `ALAC_Archive/2026-09-04/Spirit of
   the West/Spirituality (1983-2008...)` is 48 kHz / 24-bit ALAC declaring
   **237.6 s**. It decodes cleanly to **2:47** and then fails for the rest of
   the file: `Error while decoding stream #0:0: Not yet implemented in
   FFmpeg, patches welcome` ×17, then `Invalid data found`. `#0:0` is the
   **audio** stream, so this is not the cover-art false positive — and it is
   not an unsupported ALAC feature either, or it would have failed at the
   first frame rather than 70% of the way in. It is damage inside the stream:
   exactly the shape the decode audit exists to find, and exactly what would
   otherwise have been baked into a listening copy.

   A different recording of the same tune (96 kHz, from *Tripping Up the
   Stairs*) is already in ALAC-Library and decodes in full. It is a different
   album version, not a replacement.

## P2 — needs Grey's judgement, cannot be automated

3. **QUARANTINE is 3.1 GB and nobody has ruled on it.**
   `denied/` 109 files / 2.6 GB, `corrupted/` 38 files / 492 MB. 65 archive
   rows point into it (51 denied, 14 corrupted). Includes The Communards'
   "Don't Leave Me This Way" — a genuine 1986 record MusicBrainz scores 100,
   sitting in `denied/` since 2026-09-01.

4. **MUSAEUS_HOLD is 866 MB / 45 files**, outside the vault, awaiting
   `~/Desktop/MUSAEUS_HOLD_unmatched_2026-09-03.csv`. Aerosmith's
   "What It Takes" (199 MB) is the largest and is already on TuneMyMusic.csv.
   Decide keep-or-delete and the directory can go.

5. **51 DUPE_REVIEW rows**, all with a surviving copy, from three old runs.
   None was in the 170-group CSV — that generator's selection did not cover
   everything in DUPE_REVIEW.

6. **11 truncated fragments** in `~/Desktop/MUSAEUS_fragments_2026-09-06.csv`.

7. **87 artist-vs-folder pairs** in
   `~/Desktop/MUSAEUS_artist_vs_folder_2026-09-07.csv`, 24 already marked.
   Only 9 are real drift; the rest are fuller credits, case, or the and/&
   rename.

8. **Three artists held** from ArtistsToReview.csv — Huey Lewis & The News
   (genre "Pop  Rock" is not in the vocabulary), Maurice Williams & the
   Zodiacs (rename target looks reversed), Simon (-> "Garfunkle" is spelled
   "Garfunkel" in the library).

9. **`Dean` -> `Jan & Dean`** — 2 rows still under "Dean", 43 under the
   canonical name. MB-confirmed on artist AND title.

---

## Deferred by Grey (2026-09-05) — pin all modules at startup

**Not urgent. Do it when the pipeline is idle.**

The 2026-09-05 handoff-doc loss had a root cause deeper than the missing
doc: `cli.py` imported `handoff.py` inside a function, so a 42-hour-old
process loaded *fresh* handoff code against a *stale* `musaeus.context`.
Python caches modules in `sys.modules`, so a deferred import gets new code
with old dependencies — a mixture, not a rollback. Eager-importing
`handoff` fixed that one call site. **60 deferred internal imports remain**
(`grep -rn '^\s\+from \.' musaeus/ --include=*.py`).

**The fix:** import every module at startup, so a mid-run `.py` edit is
inert for the running process — Python then never re-reads any of them.
Measured 2026-09-05: **97 modules, 0.15s, zero failures.**

```python
import importlib, pkgutil, musaeus
for mi in pkgutil.walk_packages(musaeus.__path__, "musaeus."):
    if mi.name.rsplit(".", 1)[-1] == "__main__":
        continue          # importing __main__ RUNS the CLI — it opens the
                          # interactive console and blocks. Verified, not guessed.
    try:
        importlib.import_module(mi.name)
    except Exception as exc:          # report and continue, never abort:
        ...                           # __main__ proves this package can carry
                                      # module-level side effects, and the next
                                      # such module must not take down every run.
```

**Where it goes: `cli.py`'s startup path, NOT `PreflightStage`.** Preflight
is skippable — the 2026-09-05 dedupe ran as `musaeus run --skip ...` — and a
partial run is exactly when someone is most likely to be mid-edit. A pinning
step inside a skippable stage is missing precisely when it is needed.

This makes the *hook and the preflight check below* belt-and-braces rather
than the only defence: the hook warns a human, the pin makes the mistake
harmless.

---

## Done 2026-09-05 — edit-guard hook

`~/.claude/hooks/musaeus_pipeline_guard.sh`, wired as a Claude Code
`PreToolUse` hook on `Edit|Write|Bash`. Warns (never blocks) when a MUSAEUS
`.py` is **modified** while a pipeline actually runs.

Three false-positive sources it avoids, each found by testing rather than
review — a hook that cries wolf gets ignored, which is the failure it exists
to prevent:

1. `pgrep -f` matches its **own** command line. An un-bracketed pattern
   reported "dedupe: RUNNING" ten minutes after it finished. Uses `[m]usaeus`.
2. A shell that merely *mentions* "musaeus run" (a grep, an editor) matches
   the pattern but is not a pipeline. Confirms `/proc/PID/exe` is python.
3. **Reading** a MUSAEUS `.py` (`cat`, `grep`, `pytest`, `sed -n`) is
   harmless. Only mutating commands count — `sed -i`, a redirect/`tee`/`cp`/
   `mv` landing on a `.py`. Known gap: a python heredoc that opens a file for
   writing is not detected; shell strings cannot be parsed reliably.

`PreflightStage._check_edit_guard_hook()` verifies the hook is registered and
executable, so "it silently never loaded" is visible. Report-only, never a
FAIL, silent when Claude Code is not installed, and reads only hook *command*
strings — `settings.json` also holds env blocks and API headers. 8 tests in
`tests/test_preflight.py`.

---

## Small, deferred (2026-09-05) — needs_setup() ignores the environment

`needs_setup()` (`musaeus/setup/wizard.py`) tests only for
`~/.config/musaeus/settings.env`. It never consults `os.environ`, so
exporting `MUSAEUS_VAULT_ROOT` is not enough — every command drops into the
interactive wizard, which aborts when nothing can answer it. That blocks any
non-interactive use: containers, cron, systemd.

The Docker image works around it by seeding the file, so **the image and a
bare `pip install` now diverge**. Worth closing so that is not rediscovered
later:

```python
def needs_setup() -> bool:
    if os.environ.get("MUSAEUS_VAULT_ROOT"):
        return False          # the environment already answers the question
    if not _SETTINGS_FILE.exists():
        return True
    return not _load_env(_SETTINGS_FILE).get("MUSAEUS_VAULT_ROOT")
```

Found 2026-09-05 by running the container, not by reading the code.

---

## P3 — older execution notes (kept for the reasoning, superseded by P1/P2 above)

Ordered by "would this be painful to discover alone in October?"

1. **Re-dedupe.** dupe-resolver crashed after 5,884 moves; the remainder was
   never processed and is still CATALOGUED. The crash itself is fixed
   (`7653d6d`) but that fix is inert for the run in flight. Run it with
   `--skip` naming every OTHER stage, so only dupe-resolver runs. Derive the
   list rather than copying one -- it is right whatever the pipeline becomes:

   ```bash
   python3 -c "from musaeus.stages import DEFAULT_PIPELINE as P; \
     print(','.join(getattr(c,'NAME',c.__name__) for c in P if getattr(c,'NAME','')!='dupe-resolver'))"
   ```

   `--skip sentinel,scholar` is not enough: canonicalize, finalize, organize,
   audit and the enrichment chain are also unmarked and would re-run. The two
   skipped-for-non-reasons are Scholar's 3,133 (archive copies deliberately
   pulled) and Sentinel's 33 (GHOSTs of deleted files). Note a --skip run
   changes the stage list, so it clears the full-pipeline resume state on
   success — back that file up and restore it after.

2. ~~**Rule the no-genre rows and the advisory deny entries.**~~ **DONE
   2026-09-06.** Genre coverage is 100.0%; the deny list was cleared to
   Grey's ruling and then re-seeded with the 19 hashes he wanted blocked.
   The 18 artists behind the last blank genres were added to MasterLaw, so
   the same rows cannot come back blank.

3. **Read `~/Desktop/MUSAEUS_HOLD_unmatched_2026-09-03.csv`** — the files held
   outside the vault, most of them clean and genuinely absent from the library.
   `hold_count` in MUSAEUS_OPEN_ITEMS.md says how many remain. Decide
   keep-or-delete, then the HOLD directory can go.

4. **Port ORPHEUS's `build_aac_port_iphone.py`.** Still the only edition with no
   builder. Lowest of the four because it adds capability rather than protecting
   what exists — and after the 8th, protection is what cannot be replaced.

## ~~Known gaps in CorruptStage~~ — CLOSED 2026-09-07

**Done.** doctor's relative rule is now in CorruptStage, so it stops
fragments at ingest rather than only reporting them afterwards.
Measured on the live library: **6 of 11 caught before, 11 of 11 after.**
The two gaps below are what it fixed, kept for the reasoning.


`doctor` now REPORTS truncated fragments by comparison (commit `9e04c71`),
but `CorruptStage` is what would STOP them at ingest, and it still uses the
absolute rule. Measured against the 22 fragments doctor found: **13 caught,
9 escaped.**

1. **`MIN_DURATION_SEC = 45` is absolute.** A 48-second file sitting beside a
   4:27 copy of the same song is obviously a clip, and an absolute floor
   cannot see it. Raising the floor is not the fix -- 120s would flag 397
   complete recordings ("Hit the Road Jack" 2:00, "All Shook Up" 1:58).
   Escaped this way: Def Leppard 48s, Player 56s, Olivia Newton-John 57s,
   Mr. Mister 59s, Eagles 47s.

2. **The keyword exemption fires blind.** `intro|outro|skit|reprise|
   interlude|snippet|clip|bonus|fragment|excerpt|medley|bridge` exempt a file
   regardless of context. For the Eagles' "Doolin-Dalton (Reprise II)" that is
   probably right; for Elvis "Can't Help Falling in Love (Epic intro)" at 21s
   beside a 2:57 copy it is plainly wrong.

**The fix is to port doctor's relative rule into CorruptStage**: near-zero
(under 3s) OR under 60s when another copy of the same recording runs past
120s. It ignores absolute length and keywords entirely, which is exactly why
it survives both cases above. ~1 hour with tests.

## P4 — with Kiro, over the 30 days

**The ORPHEUS harvest.** ~260 scripts, read-only, exploratory, and it continues
after the 8th. Point it at `/mnt/FORGE2TB/Projects/ORPHEUS` — never at
`MUSAEUS_VAULT`.

**Do not spend the last Claude days on ORPHEUS archaeology.**

## Done 2026-09-06

A long day with Grey present throughout. Three commits, all pushed:
`399d56b` (doctor), `f7a6839` (Rock N'Roll), `98c4e87` (tagger),
`2c735e5` (tributes, local at time of writing).

**Space and data**
- **18.88 GB freed** — 484 untracked files in the dupe holding area, 481
  verified as surviving elsewhere, 3 tribute acts.
- 6 catalogued rows repaired whose files the dupe-apply had deleted: the
  review CSV listed one path in two groups, so 23 paths carried both DELETE
  and KEEP and the apply honoured both, reporting 0 errors.
- Deleted on Grey's rulings: George Michael (18), Drake + SZA (24, with an
  explicit guard so Nick Drake was untouched), 7 tribute knock-offs,
  7 stranded karaoke files, one James Purify mislabel, two unidentifiable
  rows. Originals of the tributes added to `TuneMyMusic.csv`.

**Authorities brought back into line**
- `&`/`and` convention applied library-wide (167 artists, 369 titles).
- 38 MasterLaw keys swept to match, which would otherwise have gone dormant.
- `Rock & Roll` → `Rock N'Roll` across five places, one of them a tuple in
  `editions.py` that no check reads.
- `Classic Rock` admitted; `Baroque`, `Karaoke`, `Pop, Rock` retired.
- Deny list emptied on Grey's ruling, then 19 hashes re-added as BINDING.
- **doctor: genre coverage 100.0%, artists with >1 genre 0, authorities agree.**

**Three bugs that no count would have shown**
1. `doctor` reported `library files with no row: 0` beside 19 GB it could not
   see — it scans ALAC-Library and the review folders live under
   ALAC_Archive. New reciprocal check; it found 7 stranded files at once.
2. `tagger` rewrote 3,161 files on **every run, forever**: two rules owned
   `artist` and disagreed, so a correct file was clobbered with the sort form
   and the next run put it back. Four runs reported 3997/3161/3161/3161.
   Now `changed=1`. Found only by running the stage twice.
3. `tagger`'s `verify_effect` compared the natural-form tag against the row's
   sort form, so it had been red for every article artist — a check nobody
   would read.

**The pattern.** Every one of these was two places holding the same fact and
disagreeing. That is the thing to keep looking for.

## Deliberately not doing

- **A cover-detection rule.** Agreed 2026-09-03: in if it's the artist's own
  work, OR if it charted for whoever released it. That needs songwriter-vs-
  performer data plus chart history, and jazz/blues standards are exempt by
  design. Springsteen's own *Blinded by the Light* never charted; Manfred
  Mann's went to #1. A rule would destroy the first. This stays a per-record
  judgement.
- ~~**Extending tribute-quarantine to tribute-concert albums.**~~
  **REVERSED 2026-09-06 on Grey's instruction** ("Musaeus knows to reject
  karaoke, please add tributes to that rule"), commit `2c735e5`. The old
  reasoning — that matching the artist name was enough — was wrong: three
  knock-offs were CATALOGUED whose artist names are innocent ("Classic Blues
  Tones", "Scott D. Davis", "Led Zepagain") and only the TITLE said tribute.
  Grey found them by eye. `\btribute\b` is now a title pattern too, with
  Bruce Springsteen and the LA Three Tenors added to PROTECTED_ARTISTS
  because both genuinely performed AT a tribute.

  Still deliberately not done: a bare `cover` pattern. See the cover rule
  above — it would have quarantined Springsteen and Morse/Portnoy/George.
- **Automating the remaining no-genre rows.** Two rounds of mechanical recovery
  returned one row each.
- **Re-enabling the overnight cron.**
- **Reviving ORPHEUS as a running pipeline.**

## Standing hazards

- **A long-running process runs the code it imported at startup.** Six fixes
  committed 2026-09-03 are inert for the run in flight.
- **Measure the artifact, not the report** — and "decodes clean" is not
  measuring it. A re-encode OF truncated audio decodes perfectly. Compare
  duration; where it matters, compare the PCM hash.
- **Silence is not evidence, and neither is noise.** A seal that cries wolf is
  discarded as fast as one that lies. Verify what the row CLAIMED.
- **Check the model before trusting a session with vault mutations.**
  `echo "${ANTHROPIC_BASE_URL:-unset}"` — unset means direct to Opus 5.

---

## A note on this document

It deliberately does not restate live counts. Four such claims across this
file and MUSAEUS_OPEN_ITEMS.md went stale within 24 hours of being written on
2026-09-04 -- a file count, a command, a staleness date, and three CSV rows --
and none of them announced that they had. Reasoning ages well; state does not.

So: history and rationale in prose, anything countable behind a command. The
helpers live at the end of MUSAEUS_OPEN_ITEMS.md, and `musaeus doctor` is
better than any of them.
