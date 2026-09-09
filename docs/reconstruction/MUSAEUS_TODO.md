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

3. **PARTLY CLOSED 2026-09-08 — `corrupt` and `bitrot` now preview.** The two
   the note below picks out as worth doing first both have a
   `plan_candidates(conn, cfg)`, and both are reachable from the real CLI:
   `musaeus dry-run` shows corrupt, `musaeus bitrot --dry-run` shows bitrot.
   Ten stages had one; twelve do now. The remaining 19 still print
   `no preview available`, and everything the entry says about them stands.

   Measured against the live vault at the time of writing:

   ```
   corrupt    16,103  CATALOGUED tracks to scan; at most 200 never-checked
                      files are decoded per run
   bitrot     15,813  15813 file(s) to hash; 0 have no baseline (100.0%
                      covered) — a verify reports those as new, not as corrupt
   ```

   **bitrot's preview leads with baseline coverage, not with a total.** That
   is the number the 2026-09-08 verify pass proves you need: an unbaselined
   file is reported as *new*, not as corrupt, so a mostly-unbaselined archive
   returns a green verify having compared nothing. The 100.0% above is the
   rebaseline holding; the figure independently reproduces the handoff's
   15,813, from a directory walk rather than from the same query.

   Arity was guarded only across `DEFAULT_PIPELINE`, which does not contain
   bitrot — a guard that could not fire for the one standalone stage. There is
   now a second check over every stage that defines the hook (12 found).

   ~~**`--dry-run` previews nothing for 21 of the 31 stages.**~~ It prints
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

4. ~~**Run `~/musaeus_jobs/recheck_decode_failures.sh` once.**~~
   **DONE 2026-09-08. The library has been decoded end to end.**

   **16,107 of 16,107 CATALOGUED files decoded in full. 8 are damaged —
   0.05%.** The sweep flagged 11; the recheck cleared 3 as cover-art false
   positives, and every surviving error names the audio stream or the
   container, none the artwork.

   Ruling CSV: `~/Desktop/MUSAEUS_decode_damaged_2026-09-08.csv`.

   | artist | title | verdict |
   |---|---|---|
   | Carlos Santana | Bella | no other copy — re-source |
   | Daryl Hall & John Oates | Kiss on My List | only an 85s copy against 265s |
   | Foreigner | Feels like the First Time (2008 Remaster) | only a 247s copy against 161s |
   | M/a/r/r/s | Pump Up The Volume (UK 12" Remix) | only a 308s copy against 389s |
   | Rage Against the Machine | Testify | replaceable — clean copy, same length |
   | Spirit of the West | Homelands [Jigs…] | replaceable |
   | Who, The | Cut My Hair | replaceable |
   | ZZ Top | Legs | replaceable |

   **Read the middle three carefully before deleting anything.** A first
   pass said all seven had "another clean copy" and that was wrong: a
   same-title match is not a same-recording match. An 85-second file is not
   a replacement for a 265-second song, and a 12" remix is not its own
   single edit. Only a length match within 5% earns the word replaceable.

   **RESOLVED 2026-09-08 — the four replaceable copies are deleted.**
   Manifest: `~/Desktop/MUSAEUS_deleted_damaged_2026-09-08.csv`. Every
   keeper was re-decoded in full immediately before its damaged twin was
   removed, and matched on duration to within 0.03 s. The four unreplaceable
   ones are untouched and still need a ruling.

   **A second selection bug, caught only because deletion is irreversible.**
   The CSV named the wrong survivor for Spirit of the West. It picked the
   *first* candidate inside the 5% window — a 241.2 s, 44.1 kHz/16-bit copy
   — when the real match was a **237.59 s, 96 kHz/24-bit** copy from
   *Tripping Up the Stairs*, 0.03 s from the damaged file. Both passed the
   window; only one is the same recording, and it is also the better master.

   The tolerance test answered "is there something close enough?" when the
   question is **"which of these is the same recording?"** Those differ
   whenever more than one candidate qualifies. **Any future replaceability
   check must rank candidates by closeness and show the runner-up**, not
   return the first hit. A 5% window on a 4-minute song is ±12 seconds —
   easily wide enough to admit a different take, an edit, or a remix.

   **Side finding worth its own look:** that 85-second *Kiss on My List* is
   itself almost certainly a fragment, and `doctor`'s truncated-fragment
   check will never say so — it flags files under 60s beside a sibling over
   120s, and 85s falls in the gap between those bounds. Widening them is
   what the 120s-floor measurement already ruled out (397 complete
   recordings flagged), so the gap is deliberate, not an oversight. It does
   mean fragments between 60s and 120s are invisible to that check.

6. ~~**Run `~/musaeus_jobs/recheck_decode_failures.sh` once, after the
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
   either way; a wrong entry there costs a second look, not a file.~~

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

## P1b — the CAR build's 60 errors (reviewed 2026-09-08)

Reviewed on Grey's question "anything negative here to report?". Log:
`~/Desktop/MUSAEUS_car_build_2026-09-07.log`. The run that resumed
2026-09-08 04:55 UTC: **3,465 converted, 8,437 already done, 60 errors** —
0.5%. All 60 are from this run, not carried over from an earlier one.

**The good news first, and it is genuinely good.** Three of the errors are
`verification failed: duration mismatch`:

| | source | output |
|---|---|---|
| Carlos Santana – *Bella* | 268.2 s | 143.8 s |
| M/a/r/r/s – *Pump Up The Volume (UK 12" Remix)* | 389.1 s | 298.0 s |
| Rage Against the Machine – *Testify* | 210.1 s | 91.3 s |

**All three are on the decode audit's damaged list.** Two entirely
independent methods — a full decode, and a source-vs-output duration
check — found the same three files. That is the strongest evidence in this
project that the decode audit measures something real.

It also shows the CAR builder already had the guard the ALAC bake lacked
until today: it verifies its output against its source and REFUSES rather
than shipping a truncated encode. The pre-bake gate added in `57b9209`
brings the ALAC tier up to the same standard.

**The other 57 are not content problems.** 58 of the 60 error sources decode
clean under a full audit. What is wrong is naming:

- **at least 8: the artist is truncated at `&`.** `Echo & the Bunnymen – The
  Killing Moon` was written to a folder called `Echo`, as
  `Echo - The Killing Moon.m4a`. Same for `Daryl Hall & John Oates`,
  `Earth, Wind & Fire`, `Gladys Knight & The Pips`,
  `Benny Goodman & His Orchestra`. This is Grey's ampersand ruling appearing
  in a third place: **`&` joins artist names and must not be split** when it
  is part of the band's own name.
- **14: the `(N)` dedupe suffix is stripped**, so `... (3).m4a` and `....m4a`
  claim the same output path. **13 of those 14 are already in the car edition
  under the un-suffixed name**, so almost nothing is actually lost here.
- **~35 not root-caused.** The output path is correct and the source decodes,
  but no `.bake_tmp` was produced, so ffprobe reported it missing. Said
  plainly rather than guessed at.

**What it costs, concretely: 32 of the 60 are still absent from the car
edition.** The other 28 are present. Nothing is lost from the library
itself — every one of those 32 remains in ALAC-Library — the car copy is
simply not there.

**Not urgent.** The car edition is the disposable tier: it is rebuilt from
the masters whenever wanted, and being 32 tracks short of 11,900 does not
threaten anything. Fix the ampersand split before the next full CAR rebuild
and most of it goes away.

## P1c — Repair Register triage (2026-09-08)

Thirty findings, `~/Downloads/MUSAEUS Repair Register.pdf`. Triaged and
ranked below so this can be picked up cold. **Ranked by what it costs if left
alone**, not by how hard it is.

Two of the Register's own framing claims are wrong and are corrected here,
both verified: the vendored files are **not** byte-identical (ORPHEUS 461
lines, MUSAEUS 799, patched 2026-08-16 — syncing them would clobber those
patches), and **M-01 does not exist in ORPHEUS at all**. Check each O-finding
against the MUSAEUS copy before assuming it applies twice.

### Tier 1 — loses data, or ships silently wrong audio

| id | state | what it costs |
|---|---|---|
| **M-01** | **FIXED `88ecf5c`** | an unreadable source deleted its own good encode |
| **M-02** | **FIXED `3bb430b`** | **the Register's figure was an inference and is wrong by three orders of magnitude — see below.** Original entry: **the biggest one left.** The resume check compares duration *only*, so every output encoded before the `-ar` cap and `-ac 2` downmix reports `SKIP DONE`. By the code's own docstring that is **4,862 of 10,545 files above 48 kHz, 4,223 of them at 192 kHz**. The car edition is silently wrong for thousands of files and **a normal re-run will never fix them**. Fix: compare sample rate and channel count too — ask "is this what the current settings would produce", not "is something roughly this long here". |
| **M-05** | **FIXED `3363bf2`** | `--dry-run` is nested inside `if args.from_catalogue:`, so a dry run over hand-dropped files takes the else branch into a real ~44-hour encode, with masking and DB writes. A safety flag that does not stop anything. `--limit` and `--budget-gb` are ignored outside that branch too. |
| **M-14** | **FIXED — and it was CONFIRMED, not plausible** | leaked `_staged_<pid>` trees were never cleaned after a successful build, and `find_input_files()` excluded `_output` but not them, so a later non-catalogue run re-ingested the whole catalogue. **Measured 2026-09-08: 3 leaked trees, 41,811 symlinks, 0 genuine dropped files — the script reported 41,031 inputs for a library of ~16,000.** Found while *verifying M-05*: its new dry-run guard printed the 41,031. Before M-05, that same command would have encoded them. |
| **O-01/O-02** | **FIXED `ac0d1ef`** (MUSAEUS copy) | the noise chain gates on `.exists()`: a truncated or 96 kHz bed is accepted and mixed under all ~10,000 tracks. The generator grew `_is_good_track` for exactly this; the consumer never did. **`copy_noise_tracks` now validates each bed and refuses by name. Two further M-12-shaped holes closed with it: an unprobeable bed took the raw-copy branch precisely because nothing could tell what it was, and a failed re-encode copied the source as-is — shipping the 96 kHz file the re-encode existed to replace. ORPHEUS's own copy is untouched; see below.** |


**O-01/O-02 left one thing open, and it is worth knowing (2026-09-08).**
`orpheus_noise_generator._decodes_cleanly` — the check the consumer now reuses —
is a surviving copy of the "any stderr means damage" rule that `c9542c4` fixed
elsewhere. It passes no `-vn` and treats any stderr as damage, so a file with
broken cover art and perfect audio reads as damaged. It is **correct for the
beds it faces** and that is measured, not assumed: all six in `RUNS/Noise` probe
as a single audio stream, no artwork, 44.1 kHz. It is wrong in general.

Not fixed here for two reasons. The file is shared with ORPHEUS (`SCRIPTS/`
carries the same function), so changing it is the sync question rather than a
bug fix; and the corrected version lives in `musaeus.duration`, which this
vendored tree deliberately does not import. **If a noise bed ever carries
artwork, this check will call it damaged — loudly and wrongly, but never
silently**, which is the right way round for a bed that goes under everything.

**M-02, measured after the fix (2026-09-08).** The Register cites *"4,862 of
10,545 files above 48 kHz"* as the damage. That is a count of **sources that
trigger the cap** — the live DB says 5,370 above 48 kHz, 4,465 of them at
192 kHz — **not** a count of wrong outputs. Probing all **15,891** encoded
files found:

| | |
|---|---|
| outputs above 48 kHz | **0** |
| outputs with more than 2 channels | **3** |

The rate cap is working on encode. What M-02 actually left behind is a stale
pre-cap remainder of **three files**, all 48 kHz 5.1, all now correctly
refused by the fixed check:

- `Hoobastank - The Reason` — source 48 kHz/6ch
- `Billy Squier - The Big Beat` — source 48 kHz/6ch
- `Beck - The Paisley Experience` — **source 192 kHz/6ch, output 48 kHz/6ch**

**The Beck file is NOT evidence for M-12 — checked, and the plain
explanation wins.** Its rate was capped 192 → 48 while its channels were not
downmixed, which looked like M-12's signature (the two flags come from two
independent probes, and M-12 says either vanishes when its probe returns
None). The timeline refutes it:

| | |
|---|---|
| `-ar` cap added | **2026-08-31** (`f70c6ad`) |
| all three offending outputs encoded | **2026-09-01** |
| `-ac 2` added | **2026-09-02** (`bbdd4db`) |

All three were encoded in the one-day window when `-ar` existed and `-ac`
did not. The asymmetry is chronology, not a probe failure. **M-12 remains
PLAUSIBLE and still needs reproducing on its own terms.**

Recorded because the wrong version of this note was committed first, and the
check that overturned it — compare the file's mtime against the commit that
added the flag — took about a minute. A finding that "looks like the shape
of" a known defect is a hypothesis, not evidence for it.

**The repair is three files, not a rebuild.** The defect was real — a blind
check hides its misses for ever — but knowing the size changes what you
schedule. A normal `--from-catalogue` run will now redo these three.

**A note on measuring it.** The first offender scan reported one file; an
independent count said three. The scan was simply still running — but its
script also exited silently whenever a probe failed, so it *could* have
under-reported and nobody would have known. It was rewritten to emit
`PROBE_FAILED` instead of skipping, and re-run clean: 3 offenders, 0 probe
failures, agreeing with the count. **A measurement script needs the same
"report your coverage" discipline as the checks it is measuring.**


~~**The three leaked trees are still on disk and now harmless.**~~ **DELETED
2026-09-08 on Grey's word.** `find_input_files()` skips them by name, so nothing
would have walked into them again; the cleanup only removes *this* run's tree,
deliberately — sweeping other PIDs' trees is what caused the 2026-09-01 incident
where a dry run deleted the staging a live build was reading from.

Removed by hand instead, with all five §7 jobs confirmed clear first, so no build
was reading from them. Measured immediately before deletion, and it matched the
Register's figures exactly:

| tree | symlinks | regular files | size |
|---|---|---|---|
| `_staged_2384574` | 15,684 | **0** | 101 MB |
| `_staged_3083696` | 15,684 | **0** | 101 MB |
| `_staged_34543` | 10,443 | **0** | 74 MB |
| | **41,811** | **0** | **276 MB** |

Zero regular files in any of the three: nothing but symlinks and the directories
holding them, every link pointing out to `Libraries/ALAC_Archive/`. `rm -rf` on a
symlink removes the link and never the target, and a canary target was stat'd
before and after to prove it — `MGMT — Time to Pretend`, 72,294,065 bytes, byte
identical afterwards. `_output/` was not touched.

### Tier 2 — guards that cannot fire

The §5 pattern, five more times. Each of these *looks* like protection.

| id | state | what it costs |
|---|---|---|
| **M-03** | **FIXED** | line 545 inlines `max(1.0, src * 0.02)` while `_DURATION_TOLERANCE_SEC = 2.0` sits at line 142, and the comment claims they agree. A 30 s track drifting 1.4 s on AAC priming is **accepted at write time and rejected on the next run — deleted and re-encoded for ever**. The guarding test greps for the constant and is structurally blind to an inline literal. `musaeus/duration.py:63` already has `tolerance_for()`; call it. |
| **M-04** | **FIXED** | `is_protected('Andrews Sisters (the)')` is True; `'Andrews Sisters, The'`, `'The Andrews Sisters'` and `'Andrews Sisters'` are all False — and `normalize.py` actively rewrites the working spelling into the dormant one. `genre_law._key()` already folds all three article forms and its docstring records that 246 rules were dormant for this exact reason. The test pins the dormant spelling, cementing it. |
| **M-10** | **FIXED** | `PROTECTED_ARTIST_NAMES` exists in two modules with **disjoint** contents, so the "one home" guard — which keys on overlap ≥ 2 — can never fire. `normalize.py` runs `UPDATE archive SET artist=?` and imports nothing from canon. |
| **M-09** | **FIXED** | `_load()` clears `_allowed` but not `_allowed_lower`, so a reload after the file disappears raises `ValueError` instead of returning None. |
| **M-11** | **FIXED** | the ERROR-severity semgrep rule has **never scanned `tests/`** — semgrep's bundled defaults exclude it and `--no-git-ignore` does not lift it. Naming a file directly returns five real hits. |


**M-04, measured (2026-09-08).** Of the thirteen canon entries, **exactly one
was dormant** — `andrews sisters (the)` — and the library stores that artist
as **"Andrews Sisters, The", 4 rows**, which is precisely the spelling that
returned False. The other twelve matched their stored spelling already, so
the blast radius was one artist and four files.

The *failure mode* is the systemic part, and it is why this is worth more
than four rows: `normalize.py` rewrites names **into** the suffix form, so
the pipeline actively converted the one working spelling into a non-working
one before anything asked whether it was protected. A dormant rule looks
exactly like an absent one.

`is_protected()` now folds through `GenreLaw._key()` rather than restating
the rule — two modules folding differently *is* the incident, and GenreLaw's
own docstring records 246 genre rules dormant for the same reason. A test
pins the coupling so they cannot drift apart again, and another runs the
whole live artist list through it.

`&` is deliberately **not** folded: "Of Monsters and Men" spells its own name
with "and", and a blanket ampersand fold would protect a name nobody listed.


**M-10, and a correction to how it was described (2026-09-08).** The Register
says the one-home guard "cannot fire". It fires correctly for what it guards
— duplicated **contents**, needing `_MIN_SHARED_ENTRIES` matches — and the two
sets share *no* entries, so it had nothing to see. It answered its own
question right; nobody had asked the other one.

The two lists are **genuinely different concepts**, so they were renamed
rather than merged, exactly as the Register's own repair note allows:

| module | guards against | example |
|---|---|---|
| `canon/protected_artists.py` | an ampersand band being **split** | `Hall & Oates` |
| `stages/normalize.py` → now `ARTICLE_LOOKALIKE_ARTISTS` | a foreign article being **moved to the suffix** | `De La Soul` → `La Soul, De` |

**Nothing was actually broken.** Measured: `_normalise_artist` returns "no
change" for twelve of the thirteen canon names, and the thirteenth is the
article repair, which is intentional and — since M-04 — still protected
afterwards. The hazard was a reader importing the wrong one and getting a
guard that is silently empty for the names they meant. This project's
recurring shape.

A new guard now keys on the **identifier**, not the contents, and it was
proved red-then-green against a deliberately reintroduced collision before
being kept.


**M-03 (2026-09-08).** There were **three** rules, not two:

| where | rule | 30 s track, 1.4 s drift |
|---|---|---|
| `_verify_bake` | flat `2.0`, no scaling | **accepts** |
| `_output_matches_source` | inline `max(1.0, src * 0.02)` | **rejects** |
| `musaeus.duration.tolerance_for` | `max(2.0, recorded * 0.02)` | accepts |

So a track that AAC priming drifts by 1.4 s was **accepted at write time and
rejected on the next run** — deleted, re-encoded, and reported `CONVERTED`,
every run, for ever.

Two things hid it. The resume check's comment claimed it asked *"the same
thing `_verify_bake` asks of a fresh encode"*, which was false — a claim that
reads as verification and is decoration. And the guarding test greps for
`_DURATION_TOLERANCE_SEC\s*=\s*([0-9.]+)`, finds the `2.0` on its own line,
and passes: **an inline literal is structurally invisible to it.**

Both call sites now use one `_duration_tolerance()`, a deliberate **mirror**
of `musaeus/duration.py` — this file is vendored ORPHEUS code that runs
standalone and cannot import `musaeus`. A test pins the two copies across ten
values, which is the only thing keeping them honest. The `1.0` floor is gone:
it appears in no ruling, and 2026-09-02 settled 1.5-vs-2.0 at 2.0.

The new guard parses the file rather than grepping it, and was proved
red-then-green. A first draft *did* grep, and failed on the docstring of the
very function that fixes the bug — a guard that cannot tell code from prose
is the same class of mistake as one that cannot see an inline literal.


**M-09 (2026-09-08).** Reproduced against the real class before touching it:
remove `Genre_Allowed.txt`, call `reload()`, and `_allowed` holds 0 entries
while `_allowed_lower` still holds the previous 3. The next lookup raises
`ValueError: zip() argument 2 is longer than argument 1`, where the contract
says return `None`.

`_allowed_lower` is now derived **outside** the `exists()` branch, so the two
cannot disagree by construction rather than by discipline.

**The `strict=True` zip is not the defect and was kept.** It converted a
silent mis-pairing into a loud one; without it the fuzzy matcher would have
scored each genre against some *other* genre's lower-cased text and returned
a confident wrong answer. Worth naming, because almost everything else in
this register is a guard that could *not* fire — this is one that did.

Also corrects the module docstring, which named
`genre_allowed.txt` / `genre_map.tsv<TAB>`. Neither has ever existed. The
loader's own comment records what that cost: the real map has used `" => "`
since it was written, so **not one of its 51 rules ever loaded**, and with no
allowed file either, `resolve()` returned `None` for every genre ever passed
to it — GenreCanon was wired into EnrichStage and doing nothing at all.


**M-11 (2026-09-08).** Reproduced exactly: the documented command scanned
**136 files, none from `tests/`**, and returned clean. An ERROR-severity rule
had never examined the tree it actually matches in, and the passing exit code
asserted coverage that did not exist.

Fixing it exposed a **second gap the Register did not mention**: the same
defaults were hiding `scripts/car_library/vendor/`, so three findings in the
vendored ORPHEUS code had never been reported either.

The command now scans **296 files, 160 from `tests/`**, and is clean —
clean *because it looked*. Ten matches in `tests/` are suppressed inline,
each carrying `# nosemgrep: <rule-id> -- <reason>`; a test refuses a bare
`nosemgrep`, so every exception is a recorded decision. `vendor/` stays
excluded but explicitly, with its reason written down and its duplication
pinned by `test_car_duration_tolerance_is_one_rule.py` instead.

**One trap for whoever runs these tests.** `conftest.py` redirects `HOME`
before any import so the real credentials file cannot leak into the suite —
correct, and it stays. But semgrep is installed under `~/.local`, so under
the fake HOME its launcher cannot import itself, returns empty stdout, and
that reads as "no findings". A first draft of the new test took that at face
value: **a test about a scanner that silently scans nothing, itself silently
scanning nothing.** It now takes the real home from the password database
rather than the environment, and treats empty output as an error.

### Tier 3 — operator-facing and robustness

| id | state | what it costs |
|---|---|---|
| **M-06** | **FIXED** | `MUSAEUS_FORCE_REENCODE=0` turns force-reencode **on** — `bool("0")` is True. Setting it to `0`/`false`/`no` to *disable* the override triggers a full 10,545-file re-encode. Verified 2026-09-08. Worth grepping both repos for the pattern. |
| **M-07** | **FIXED** | a comment tells the operator to use `--force`; the script defines no such flag, and never names the env var that does work. |
| **M-08** | **FIXED** | three ffprobe helpers dropped the `timeout=30` their sibling in the same file uses. ffprobe blocks for ever on a truncated container — exactly this code's input — and with `MAX_WORKERS = 4`, four such files hang the build silently. |
| **M-12** | **FIXED — and it was real** | `-ar` and `-ac` are dropped whenever the probe returns None, producing the unpinned encode the docstring warns about (a 44.1 kHz master emerged as 96 kHz AAC, measured 2026-08-31). Refuse the file instead. |


**M-06 / M-07 / M-08 (2026-09-08), fixed together — one file, one shape.**
Each is the code and the operator disagreeing about what it does.

- **M-06** — `bool(os.environ.get(...))` tests *presence*, and `bool("0")` is
  True. Setting `MUSAEUS_FORCE_REENCODE` to `0`, `false` or `no` — the three
  spellings anyone reaches for to turn an override off — re-encoded all
  10,545 files. Now `_env_flag()` reads the value; worth grepping both repos
  for the pattern.
- **M-07** — a comment named a `--force` flag the script has never defined,
  so following it produced an argparse error while the control that works
  went unnamed. **A wrong instruction costs more than a missing one.** A test
  now parses the real `add_argument` calls and refuses any comment naming a
  flag that is not among them.
- **M-08** — `probe_sample_rate`, `probe_channels` and `_probe_duration`
  dropped the `timeout=30` their sibling `_probe` has always carried. ffprobe
  blocks for ever on a truncated container, which is this script's diet, and
  with `MAX_WORKERS = 4` four such files exhaust the pool and the build stops
  with no output and no error. **A hang is the worst failure available: it
  looks like slow progress.** Every helper is now asserted to have a deadline,
  per-helper, so a new one cannot be added without one.

**The M-07 test caught my own fix.** My first replacement comment explained
the history and named `--force` while doing it — so the guard flagged it,
correctly. The history moved to git and this file; the comment now only says
what works. That is the right division: a comment's job is to instruct, not
to narrate.


**M-12 (2026-09-08) — confirmed, and it is subtler than "the flags are
dropped".** A failed probe does not produce a *wrong* rate; it produces
**no `-ar` at all**, because both flags are emitted conditionally and both
probes answer `None` on any ffprobe failure. `car_sample_rate`'s own
docstring says what silence costs: *"an unpinned encode takes the FILTER's
rate, not the source's. Measured 2026-08-31: a 44,100 Hz master came out as
96,000 Hz AAC."*

`car_sample_rate(None) → None` is correct on its own terms — "unreadable: do
not guess". **The defect was the caller reading "do not guess" as permission
to proceed.** It now refuses, and `convert_one` turns that into one ERROR
line for that file while the build carries on.

**A defect I introduced this morning, found while fixing this one.** M-08's
`timeout=` stopped the probes hanging — but none of them caught
`TimeoutExpired`, so a fired deadline became an uncaught exception landing on
`convert_one`'s broad `except Exception` three frames away, where it reads as
a mystery rather than as an unreadable file. Each probe now answers with its
own documented "unreadable" value, which the M-12 guard then refuses. **One
way for a file to be unmeasurable, one response to it.**

That is worth stating plainly: a fix that converts a silent hang into a
generic error is an improvement and still not finished.


### M-15 and M-16 — findings my own triage missed

Both were in the Register and absent from the triage above until 2026-09-08.
Recorded plainly: a triage that silently drops two of thirty is the same
failure as a check that scans nothing.

**M-16 — FIXED. Two rules that matched only their own fixtures.** Verified by
probe file, not by reading:

| form | before | after |
|---|---|---|
| `re.compile(r"^(The\|A\|An)\s+")` | missed | caught |
| `re.sub(r"^The\s+")` (capitalised) | missed | caught |
| `[([{` unescaped class | missed | caught |
| the two fixture spellings | caught | caught |

Both rules passed their own validation while missing the codebase. The
seventh instance today of *a check that finds nothing is not a check that
found nothing wrong.*

The broadened rules immediately found real code the old ones could not see: a
hand-written **nine-language** article regex in `musaeus_upgrade_check.py`.
It is **not** replaced by `artist_form.comparison_key`, which handles English
only — substituting would silently narrow the match. Suppressed with the
reason and left here as an open ruling: **should `artist_form` grow a
multi-language comparison key, or should this script keep its own?**

Two mistakes worth keeping. The first broadened bracket pattern produced a
**false positive** on `\[([^\]]+)\]` — an escaped literal plus a group, not
a multi-style class; fixed with a lookbehind requiring an unescaped opening
bracket. And the first suppression sat at the top of a six-line explanation:
**semgrep reads only the line immediately above a match**, so the marker was
never seen. The guard caught both.

**M-15 — premise disproved, decision left to Grey.** The ruff exclusion says
the vendored tree is *"kept byte-identical to upstream so it can be re-synced
without conflicts."* Measured: upstream's `build_aac_library.py` is **461
lines**, this copy is **980**, and seven of today's fixes are in it.
Byte-identity is not recoverable. Cost of the exclusion, measured the same
day: **16 ruff errors nobody has seen** — 6 non-pep585, 5 non-pep604, 3
unsorted-imports, 2 deprecated-import; all cosmetic, 14 auto-fixable.

The Register is right that holding both is the worst of each — no re-sync
**and** no lint. The false rationale is corrected in `pyproject.toml`.
**Deleting the exclusion is a one-line change that reformats 980 lines of
vendored code, so it is a policy call and is left for you.**

### Tier 4 — documentation

- **M-13 — FIXED 2026-09-08.** The reconstruction document ordered "treat a
  stage without a meaningful preview as a defect" while this file warned that
  wiring `dry_run()` into the CLI undoes P0-02. The document now carries the
  correction and names `plan_candidates` as the right fix. **The TODO was
  right; the document was wrong.**
- Stale test count: `README.md:47` and `TESTING_ON_YOUR_OWN_FILES.md:129` say
  2,123; the real count is 2,411. Cosmetic — fix when next in those files.

### Do not chase — the Register says so itself

- **`README.md:43`'s `--dry-run` claim is correct.** The review agent ranked
  this its top finding and then retracted it.
- **The original M-11** ("nine ERROR matches") is not reproducible. The
  rewritten version above is the one to work from.

### If you are picking this up cold

Do **M-02** first. It is the only open finding that is *already* wrong on
disk rather than waiting to go wrong, and the files it affects cannot be
repaired by re-running the build — the broken resume check is what hides
them. Everything else in Tier 1 prevents future damage; M-02 is present
damage.

## P2 — needs Grey's judgement, cannot be automated

3. ~~**QUARANTINE is 3.1 GB and nobody has ruled on it.**~~ **DONE — this
   entry was stale.** Re-measured 2026-09-08: **22 MB, 1 file.** Grey was
   right that it had been dealt with; this document never learned it. The
   original text follows.

   ~~QUARANTINE is 3.1 GB and nobody has ruled on it.~~
   `denied/` 109 files / 2.6 GB, `corrupted/` 38 files / 492 MB. 65 archive
   rows point into it (51 denied, 14 corrupted). Includes The Communards'
   "Don't Leave Me This Way" — a genuine 1986 record MusicBrainz scores 100,
   sitting in `denied/` since 2026-09-01.

4. ~~**MUSAEUS_HOLD is 866 MB / 45 files**~~ **DONE — the directory no
   longer exists anywhere under `/home/grey` or `/mnt/FORGE2TB`.** Re-checked
   2026-09-08. Stale entry; original text follows.

   ~~MUSAEUS_HOLD is 866 MB / 45 files, outside the vault, awaiting
   `~/Desktop/MUSAEUS_HOLD_unmatched_2026-09-03.csv`. Aerosmith's
   "What It Takes" (199 MB) is the largest and is already on TuneMyMusic.csv.
   Decide keep-or-delete and the directory can go.

5. **51 DUPE_REVIEW rows**, all with a surviving copy, from three old runs.
   None was in the 170-group CSV — that generator's selection did not cover
   everything in DUPE_REVIEW.

6. **11 truncated fragments** in `~/Desktop/MUSAEUS_fragments_2026-09-06.csv`.

7. **artist-vs-folder pairs — STILL OPEN, and the count is unreliable.**
   Re-checked 2026-09-08. A quick live pass returned 116, but its path
   parsing lands on album folders in some layouts, so **treat neither 87 nor
   116 as authoritative — rebuild the CSV**. One clean sub-check does hold:
   **17 rows across 8 artists** (Benny Goodman, Cab Calloway, Coleman
   Hawkins, Glenn Miller, Billie Holiday, Bing Crosby, Louis Armstrong,
   Wynonie Harris) have a path containing `& His Orchestra` and an artist tag
   that does not — the tag was truncated at the `&`.

   **That is the same ampersand split as the CAR build's folder truncation**
   (`Echo & the Bunnymen` → a folder called `Echo`, P1b above). Two
   authorities losing the same `&`, which makes it a single root cause worth
   finding rather than two lists to hand-correct. Original entry follows.

   ~~87 artist-vs-folder pairs in
   `~/Desktop/MUSAEUS_artist_vs_folder_2026-09-07.csv`, 24 already marked.
   Only 9 are real drift; the rest are fuller credits, case, or the and/&
   rename.

8. **Three artists held — STILL OPEN, re-checked 2026-09-08.** Live counts:
   `Huey Lewis` 29 vs `Huey Lewis & The News` 4; `Tony Burrows` 5 vs
   `Tony Burrows (of First Class)` 1; `Anne Murray` 9. All three are still
   split. Original entry follows.

   ~~Three artists held from ArtistsToReview.csv — Huey Lewis & The News
   (genre "Pop  Rock" is not in the vocabulary), Maurice Williams & the
   Zodiacs (rename target looks reversed), Simon (-> "Garfunkle" is spelled
   "Garfunkel" in the library).

9. **`Dean` → `Jan & Dean` — STILL OPEN, re-checked 2026-09-08:** exactly
   2 rows under `Dean`, 43 under `Jan & Dean`. Unchanged. Original follows.

   ~~`Dean` -> `Jan & Dean` — 2 rows still under "Dean", 43 under the
   canonical name. MB-confirmed on artist AND title.

---

## ~~Deferred by Grey (2026-09-05) — pin all modules at startup~~ — DONE 2026-09-08

**Done with the pipeline idle, which was the condition Grey set.** Both P0 jobs
and all five §7 jobs were confirmed clear by PID first.

`cli.py` gained `_pin_modules()`, called from `main()` immediately after logging
is configured and **before any command dispatch** — so it covers `--skip` runs,
which is the whole reason it is not in `PreflightStage`. Failures are logged and
never abort: a pin that can refuse to start is worse than the staleness it
prevents.

**Measured 2026-09-08: 98 modules, 0.066 s, zero failures** (97 on 2026-09-05 —
the package grew by one). `musaeus.__main__` is skipped, verified still absent
from `sys.modules` afterwards; importing it runs the CLI and blocks.

Guarded by `tests/test_startup_pins_modules.py` — 6 tests, including that every
module is resident afterwards, that the count is checked against the real package
size rather than a constant that would rot, that a broken module is reported
rather than raised, and that the call sits before dispatch and *not* in preflight.

One trap worth recording: the first version referenced a module-level `logger`
that `cli.py` did not have. `python3 -m py_compile` passed it — a `NameError` is
a runtime error, not a syntax error — and it would have crashed every single
invocation. Measure the artifact, not the report: the thing that caught it was
running `musaeus -v status` and reading the output.

**Not done: the deferred imports themselves — 59, measured 2026-09-08** with the
entry's own grep. The pin makes them harmless for a running process, which is
what mattered; it does not tidy them.

Also verified on the unconfigured first-run path (`env -u MUSAEUS_VAULT_ROOT
HOME=/tmp/no-such-home`), since the pin now imports `config.py` before the
wizard can write `settings.env`: 98 modules, **zero warnings**. `_load_env()` is
a no-op when the file is absent and the `ValueError` lives in `from_env()`, not
at import — reasoned first, then measured, because the reasoning is worth
exactly nothing here on its own.

Original entry follows.

~~**Not urgent. Do it when the pipeline is idle.**~~

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

## ~~Small, deferred (2026-09-05) — needs_setup() ignores the environment~~ — DONE 2026-09-08

`needs_setup()` now returns False when `MUSAEUS_VAULT_ROOT` holds a non-empty
value, exactly as the entry below specifies. The image and a bare `pip install`
no longer diverge.

The empty-string case is treated as *unconfigured*, deliberately: the check is
truthiness on the **value**, not presence of the key. `MUSAEUS_VAULT_ROOT=` is
not an answer — the same distinction that made `MUSAEUS_FORCE_REENCODE=0` mean
*on* (M-06, fixed the same day).

Guarded by `tests/test_needs_setup_reads_the_environment.py` — 5 tests, driving
the real function with a real environment rather than asserting on its source,
since the defect was found by running the container and not by reading the code.
The fixture redirects `_SETTINGS_FILE` to a path that does not exist; without
that, the developer's own `settings.env` answers every case and all five tests
pass over nothing.

Original entry follows.

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
