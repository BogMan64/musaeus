# MUSAEUS — what is left, in priority order

**Written 2026-09-17.** Measured against the live vault, read-only, the same
day. This is the short list; the reasoning and the full counts live in
`MUSAEUS_TODO.md` under "State on 2026-09-17".

It exists as a file because the first version of it was only ever a message
in a chat window, and the PC crashed before it was read.

---

## P0 — something is unprotected right now

### 1. Bit-rot protection is at zero — **REPAIR RUNNING**

`archive_tier_hashes` held **0** baselines against **11,132** masters. It
held 1,385 on 2026-09-08; the database reset on 2026-09-10 took them.

The stage is not broken. It has nothing to compare against, so it reports
every file as *new* rather than as corrupt — a green verify that has
compared nothing. The stage's own docstring warns about exactly this.

`musaeus bitrot --rebaseline` was started 2026-09-17 and is hashing all
11,132 files (475 GB, SHA-256 plus a PCM identity per file). Log:
`bitrot.log` in the session scratchpad.

**The durable fix is still owed.** Baselines live in `musaeus.db`, which a
rebuild discards — that is how they were lost. They belong in the LEDGER
(`_db_backups/hash_index.db`), where the AcoustID fingerprints were moved on
2026-09-16 for the same reason. Until that change is made, the next reset
loses them again.

### 2. AcoustID fingerprints are at zero

`chromaprint` and `acousticid_recording` are empty for all 11,117 rows.
`fpcalc` is installed and the API key is configured, so nothing blocks it —
the stage simply has not been run since the durability fix landed, and the
LEDGER's `fingerprints` table does not exist yet.

Lower stakes than bit-rot: a fingerprint is recomputable, a corrupted master
is not. But it is one run, and after it every future rebuild costs nothing.

**Do not run it at the same time as the rebaseline** — both read every file
in the archive, and they will fight for the disk.

---

## P1 — a check is lying, or a gap is real

### 3. 24 ALAC tracks never reached the car edition

Hall & Oates' "Maneater" and "Rich Girl" among them. Not lossy-source, not
near-duplicates, not deliberately skipped — the other 386 of the 410 rows
with no car export are all accounted for.

Run `build_car_library.py --only-missing` against just these and read what
it says. If it encodes them, the resume logic has a hole worth finding.

### 4. `artist_filing.tsv` has 0 rules

Found 2026-09-17. The file is **all header** — it has never held a rule. The
"16 rules" recorded on 2026-09-14 was a line count minus a header.

So every artist files under their own tag. That is the right answer for about
95% of the library by the file's own reckoning, but the cases it was written
for — `Gladys Knight & The Pips` filed under `Gladys Knight` — are all
unhandled. Half an hour to decide whether you want any of them.

### 5. The car edition files by album artist; the library files by artist

Found 2026-09-17 while fixing three mis-filed tracks. `©ART` and `aART`
disagree on **1,038** of the 10,630 car files. Most of those disagreements
are correct and wanted — a sort-form folder, a collaboration credit — but two
kinds are worth a ruling:

- **Classical.** A concerto is tagged `©ART = Johann Sebastian Bach` (your
  ruling: classical files under the composer) and
  `aART = Salvatore Accardo, Chamber Orchestra of Europe`. The library files
  it under Bach; the car files it under Accardo. Both editions are following
  a rule, and the rules are different.
- **Junk album-artist tags.** One track had `aART = Руки'в Брюки` — a Russian
  band, on a Roy Brown record. That one is fixed; there may be others.

---

## P2 — needs your judgement, cannot be automated

6. **149 tracks with no genre** — `missing_genres_2026-09-17.csv` on the
   Desktop. None is protected by `genre_ruled_at`, so MasterLaw can fill any
   of them once the artist is ruled.
7. **101 tracks with no album name** — down from 4,913. The residue five
   sources could not agree on. Needs a person.
8. **Tony Burrows is still split** — 2 rows under `Tony Burrows`, 1 under
   `Tony Burrows (of First Class)`.
9. **One `& His Orchestra` row left** — was 17 across 8 artists.

---

## P3 — wishlist; nothing breaks without it

10. **FM-radio identifier** — in the repository with tests, never run against
    the library. It proposes into a CSV and decides nothing, so running it
    costs a review, not a risk.
11. **Discography gap detection** — in the repository. Needs a scope ruling
    first (live albums, compilations and reissues make raw output noisy) or
    it will hand you thousands of rows.
12. **Move the rest of the expensive-to-compute columns to the LEDGER** —
    BPM, musical key, energy, danceability. None has been lost yet. That is
    the moment to move them, not after.

---

## On adding a new batch of files

**Yes, and 50 → 200 → 2000 is the right shape.** The three editions are
stable, the catalogue reconciles, and the pipeline has not had new input in a
while — which is itself a reason to test it with a small batch rather than
discover the drift on a large one.

**But wait for item 1 to finish.** A batch entering today enters with no
bit-rot baseline, so if a file arrives already damaged there is no way to
tell that from damage that happened later. Fifty files will tell you whether
the pipeline still behaves; two thousand without a baseline just makes the
unprotected set bigger.

Order: **rebaseline finishes → ingest 50 → read the handoff report → 200 →
2,000.** Read the report between each step rather than at the end; the
handoff documents exist because a run that dies in Act 2 should still account
for Act 1.
