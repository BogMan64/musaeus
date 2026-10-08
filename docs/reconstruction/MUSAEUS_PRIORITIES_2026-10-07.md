# MUSAEUS — what is left, in priority order

**Rewritten 2026-10-07, evening.** Measured against the live vault the same evening. This is the short list.

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

**Nothing is running, and nothing unsafe runs on its own.** These must not be run by hand until their findings are fixed:

- `scripts/merge_case_duplicate_albums.py` (finding 87-1: it can delete a master).
- `scripts/usb_transfer/transfer_to_usb.py --no-format --dest` without `--sync` (88-1).
- The commands `scripts/iphone_transfer.py` prints for pasting (88-13).
- `scripts/write_master_loudness_tags.py` again (88-3).

---

## P1 — fixes from the October reviews, in the order agreed with Grey

1. **The stick sync** (88-1, 5, 6, 7, 8, 9), then the sync itself. The stick is back from the car; the dry run on 2026-10-07 found 53 new, 360 changed, 181 letter-case renames, 353 to delete and 8,344 already right.
2. **The findings that can move or lose a master:** 86-2, 86-6, 86-10, 86-11 (with 87-2), 86-12, 87-8.
3. **The rest of the October table.**
4. **Review the ~32,800 lines no review has read yet** (`cli.py`, `db.py`, `state/`, `canon/`, art, forge, doctor, the intake and enrichment stages), as five slices with a local `/code-review`, after Grey's weekly usage resets on Saturday.

Fixed and merged on 2026-10-07: the September findings (#81, #82), and from October 86-1, 86-3 (my own regression), 86-4, 87-3 to 87-7, 87-11 to 87-13, 88-2 (#89 to #94).

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
