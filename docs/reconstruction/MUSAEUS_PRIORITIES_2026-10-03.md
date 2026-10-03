# MUSAEUS — what is left, in priority order

**Rewritten 2026-10-03.** Measured against the live vault the same morning. This is the short list.

- The reasoning lives in `MUSAEUS_TODO_2026-09-23.md`, under "State on 2026-10-03".
- The working detail for the next session is in `~/Desktop/MUSAEUS_HANDOVER_2026-10-03/HANDOVER.md`.
- The 2026-09-23 version of this file is kept in `MUSAEUS_HANDOVER_2026-10-03/doc_backups/`.
- The file name still says 2026-09-23 so the links to it keep working.

---

## P0 — something is unsafe right now

**Nothing.** Both P0 items from 2026-09-23 are closed:

1. **The duplicate resolver moving the wrong recording.** Fixed by PR #31 (2026-09-24). Every detector records each member's audio hash. The resolver moves nothing in a group unless every member still matches it; otherwise the group is marked `stale` and reported.
2. **The PC crashing (GPU hang).** No `GPU HANG` lines in the kernel log since 2026-09-24, as far as this user can read it. The kernel is still 6.1. The upgrade proposal stands if the hangs return.

---

## P1 — running, or waiting on something running

### 1. The car edition build for the USB1 batch (running since 2026-10-03 08:15)

- 6,844 new copies, about 53 GB, about 41 hours if the machine is left alone. It pauses while the PC is in use.
- 819 copies of old masters that Act 2 replaced with better ones are removed. The songs stay, through the new copies.
- An exit code of 1 is expected: Tony Bennett "Body And Soul (Audio From Video)" fails every build (the master's length is off by 0.1 s). Grey keeps that master for now; it is on TuneMyMusic.

### 2. Then, in Grey's order

1. Check the car library for names FAT32 cannot store.
2. Copy it onto Grey's 257 GB stick: `/media/grey/5651-0F05`, already MBR + FAT32, **never reformat it**.
3. `edition-build lossless`.
4. `edition-build iphone` with **no size limit** (Grey, 2026-10-03), about 70 GB, so songs can be picked on the phone.
5. `musaeus doctor`.

### 3. FAT32-safe names everywhere (Grey, 2026-10-03)

- Forbidden characters are already handled by the shared filing rule: 0 in every library.
- **Names that differ only by letter case are not.** On 2026-10-03 there were 26 such folder pairs among the masters, for example "The Dark Side Of The Moon" and "The Dark Side of the Moon". On FAT32 they merge.
- The fix:
  - a case rule in the filing rule;
  - a one-time merge of the 26 pairs;
  - a doctor check;
  - a pre-copy check in the USB transfer.
- Grey asked for this "in Act 1". Act 1 never names library files, so the filing rule is the single place. **Grey to confirm.**

### 4. Bit-rot baselines: 0 of 9,197 masters

The 2026-09-24 wipe cleared them, and they were never regenerated. Nothing would notice silent damage to a master today. Generate them after the builds; hashing every master is a long, idle-throttled job.

---

## P2 — needs your judgement, cannot be automated

5. **Duplicate review: 3,741 songs.** They are on `~/Desktop/MUSAEUS_duplicate_review_2026-10-03.xlsx`, sorted by artist, with the kept copy beside each one.
   - 2,905 near matches;
   - 488 marked "exact" when they were set aside. None is an identical copy of a library song any more: those 2,130 were deleted on 2026-10-03. 99 of the 488 have no kept copy that can be found;
   - 323 from earlier batches;
   - 25 that sound the same.
6. **3,671 catalogued songs have no album name**, so they are filed under "Unsorted". 7 have no genre.
7. **293 catalogued masters are lossy** (not ALAC). The Lossless edition leaves them out by default. They are candidates for TuneMyMusic upgrades.

---

## P3 — improvements Grey approved on 2026-10-03, worked without Grey

Each gets its own pull request and tests. Questions come back in one list.

8. Save the audit's problems to a log file, plus a log beside the library for each run of an Act.
9. Let the tagger trust the rename rulings in `artist_canon.tsv`, so a rename fixes the album-artist tag too.
10. A `remove-artist` command that does the whole job: rows, collaborations, untagged copies with the same audio, edition copies, the deny list and TuneMyMusic.
11. The duplicate-review list as a built-in command.
12. Loudness measurements copied into the masters' tags (tags only, never audio).
13. Find out why tempo analysis ran 3× slower in the USB1 batch.
14. One ultrareview after these land. Grey will ask.

### Still open from 2026-09-23, status not re-checked

- **The safety layer (review slice #22).** Findings A and B must be fixed together. F6, the journal re-reading itself on every append, makes long runs slow. No merged fix found in the git log.
- **FM-radio identifier, discography gaps.** In the repository, never run.

---

## On running MUSAEUS end to end again

**Done.** The USB1 batch ran Act 1, then 2, then 3 (2026-10-02/03): 9,913 ingested, 6,889 filed. The audit passed after the renames. **No more catalogue wipes** (Grey, 2026-09-30). New batches go on top of the library.
