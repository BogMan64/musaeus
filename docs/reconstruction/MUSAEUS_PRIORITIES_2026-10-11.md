# MUSAEUS — what is left, in priority order

**Rewritten 2026-10-11.** Measured against the live vault the same day. This is the short list.

- The detail is in `MUSAEUS_TODO.md`, section "State on 2026-10-11".
- The working notes are in `~/Desktop/MUSAEUS_HANDOVER_2026-10-03/HANDOVER.md`.
- The Desktop copy keeps the name `MUSAEUS_PRIORITIES_2026-09-23.md` so its links keep working.

---

## The library today

| Folder | Songs |
|---|---|
| `ALAC-Archival`, the masters | 8,819 |
| `CAR_Library` | 8,819 |
| `iPHONE_Library` | 8,817 (two left out on purpose: encoder clicks) |
| `ALAC_Library`, the Lossless edition | 8,567 (252 lossy masters left out) |

---

## P0 — unsafe right now

**Nothing.** The whole program was reviewed in five slices (A–E) on 10–11 October and every
finding was fixed and merged (#148–#176), each fix with its own pre-merge review. The holds
of those days are lifted once the last chain (#169, #170, #174, #175, #176) is installed.

---

## P1 — in this order (agreed with Grey, 2026-10-11)

1. **Grey's code review** of everything merged since `3e45013`, looking at how the fixes
   work together. Brief: `~/Desktop/NOTE_FOR_CODE_REVIEW_2026-10-11.md`.
2. **Build "genre suggestions"**: MusicBrainz and Wikipedia genres, mapped to the allowed
   list, written to the Desktop as a review sheet; Grey approves; one command loads the
   approved rows into MasterLaw. It never changes a genre by itself.
3. **Grey to decide**: album names every word capitalised too (tags and folders), or
   folders only.
4. **Clear the law-set genre stamps** (`scripts/clear_law_genre_stamps.py --execute`), then
   **a full `musaeus run` over ALAC-Archival** with an empty INBOX. It applies today's canon,
   MasterLaw and filing changes in place; masters are not re-encoded.
5. **`musaeus edition-build car`**: the changed copies are retagged or moved, not re-encoded.
6. **The stick, last**: the car edition, with check-and-repair (#151).

---

## P2 — waiting

- **Next batch: USB1_Curated_RAW.**
- **One enrichment pass** after #170 (MusicBrainz releases, TitleComplete): reopen the
  rows MusicBrainz closed before #170 so their releases are found.
- **Confirm the iPhone holds the iPhone edition.**

---

## Running on its own

| What | When | Notes |
|---|---|---|
| Bit-rot check, repairs from the newest good backup | the 15th, 04:00 | next 2026-10-15 |
| Music backup to NUC8TB, checked, keeps the newest 2 | the 1st, 03:30 | next 2026-11-01 |
| Home backup (`backup-tiers/nuc_backup.sh`) | nightly 21:00, weekly Sunday 22:00 | keeps daily 14, weekly 6, monthly 1, yearly 1 |
| Backup watchdog | 11:17 and 22:30 | leaves `BACKUP_PROBLEM.txt` on the Desktop when a backup failed |
