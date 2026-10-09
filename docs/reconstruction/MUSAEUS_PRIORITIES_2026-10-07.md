# MUSAEUS — what is left, in priority order

**Rewritten 2026-10-07, evening; P0/P1 updated 2026-10-08.** Measured against the live vault the same evening. This is the short list.

- The detail is in `MUSAEUS_TODO.md`: the September and October findings tables, each finding with its status.
- The working notes for the next session are in `~/Desktop/MUSAEUS_HANDOVER_2026-10-03/HANDOVER.md`.
- The Desktop copy keeps the name `MUSAEUS_PRIORITIES_2026-09-23.md` so its links keep working.

---

## The library tonight

| Folder | Songs |
|---|---|
| `ALAC-Archival`, the masters | 8,891 |
| `CAR_Library` | 8,891 |
| `iPHONE_Library` | 8,889 (two left out on purpose: encoder clicks) |
| `ALAC_Library`, the Lossless edition | 8,633, matching its ledger exactly |

The Lossless edition was moved to `~/Music` by mistake on 2026-10-07 and copied back the same day. Every file was checked against the ledger and against the `~/Music` copy (same size).

---

## P0 — unsafe right now

**Nothing.** Updated 2026-10-08: the October findings that could lose or damage a master
are fixed (#89 to #117; statuses in the TODO table). Two scripts that could are retired
and refuse to run: `merge_case_duplicate_albums.py`, `merge_artist_folders.py` (#105).
The stick sync ran on 2026-10-07 and the stick matches the car library (8,938 files).

---

## P1 — what is left

1. **The October findings are all fixed** (2026-10-08, #89 to #128), and so are the 14
   from Grey's review of #123-#127 (2026-10-09, #129 to #133; rows R-1 to R-14 in the TODO
   table). `write_master_loudness_tags.py` is safe to run once #129 is installed; on
   2026-10-09 it would change 3 masters (0.1 GB). In `musaeus dedupe`, auto now leaves
   every group to the keep rule (#130). Grey's review of #129-#134 found 15 more (S-1 to S-15),
   fixed in #135-#139: a person's choice now follows its file by catalogue row, and edition
   copies and masters are retagged beside themselves, never in place. Three reviews in a row
   found defects in the previous round's fixes; a `/code-review` of #135-#139 after the
   Saturday reset is worth the usage.
2. **Review the ~32,800 lines no review has read yet** (`cli.py`, `db.py`, `state/`,
   `canon/`, art, forge, doctor, the intake and enrichment stages), as five slices with a
   local `/code-review`, after Grey's weekly usage resets on Saturday. Include forge, tagger and
   bpm: they still save masters in place (the 88-3 class).

---

## P2 — waiting

- **One enrichment pass** (TitleCompleteStage asks about 8,300 songs once, about 2.5 hours).
- **Confirm the iPhone holds the iPhone edition.**

---

## Running on its own

| What | When | Notes |
|---|---|---|
| Bit-rot check, repairs from the newest good backup | the 15th, 04:00 | next 2026-10-15; fixed by #90 |
| Music backup to NUC8TB, checked, keeps the newest 2 | the 1st, 03:30 | next 2026-11-01; fixed by #94 |
| Home backup (`backup-tiers/nuc_backup.sh`) | nightly 21:00, weekly Sunday 22:00 | keeps daily 14, weekly 6, monthly 1, yearly 1 |
| Backup watchdog | 11:17 and 22:30 | leaves `BACKUP_PROBLEM.txt` on the Desktop when a backup failed |
