# MUSAEUS — Masters, Editions & Delivery: the agreed vocabulary

**Decided 2026-08-31 (Grey).** Revised the same day: the first tier is
`ALAC_Archive`, the **masters**, and it is *not* LUFS-baked. Written down
before any code, because the naming is the part that has to survive.

---

> **Brought into the repository 2026-09-14.** It had lived only in
> `~/Desktop/MUSAEUS_done/`, which is why it kept failing to settle the very
> question it answers — Grey and I both wobbled between "Act" and "edition"
> on 2026-09-14, with the ruling sitting on a Desktop nobody greps. Same
> shape as `artist_form.py` the same morning: the rule existed, was correct,
> and could not be seen from where it was needed.
>
> **One correction on the way in:** this document calls the masters
> `ALAC_Archive`. The directory is now `Libraries/ALAC-Archival` (hyphen),
> and the lossless edition is `Libraries/ALAC_Library` (underscore). The
> names changed; the vocabulary did not.

> **Reviewed 2026-09-17.** Three editions now exist and the four words
> have not needed amending once. Current counts live in "State on 2026-09-17"
> at the top of `MUSAEUS_TODO.md`; this document holds the vocabulary, not
> the numbers.

## Why this document exists

Two unrelated "Act 1/2/3" schemes had grown up side by side: the
**pipeline's** Acts (`ACT1_INTAKE_CORRECTION`, `ACT2_DEDUP_STAGING`,
`ACT3_CANONICALIZE_FINALIZE`) are *intake stages*; the **notes'** Acts
(2A, 2B, 3) are *output products*. Both are legitimate. Sharing the word
"Act 3" between "canonicalize and finalize" and "copy to a USB stick" is
not. So the output side gets its own vocabulary, and the pipeline keeps
"Act".

---

## The four words

### Catalogue
The **rows**: what the pipeline knows. `archive` plus the event log. Acts
1-3 and Enrichment build and maintain it.

### Masters — `ALAC_Archive`
**Long-term home: `/mnt/NUC8TB_BACKUP`** (7.3 TB, 3.9 TB free).

The **canonical audio**. ALAC in .m4a, fully corrected -- canonicalized,
tagged, organized, identity written to the file -- and deliberately
**NOT LUFS-baked**.

This is the tier everything else derives from, and the reason for the
split:

- **Baking once, per edition.** If the masters were baked to -18, the Car
  Edition would bake -14 *on top of* -18: two loudness normalisations
  stacked on one file, the second measuring a signal the first already
  squashed. From unbaked masters each edition bakes exactly once, from a
  clean source.
- **`audio_hash` stops going stale.** Scope doc §4.24 -- "stored hashes
  are pre-bake, files on disk are post-bake" -- exists *only* because the
  hashed tier and the baked tier are the same files. With masters
  unbaked, the hash describes the master permanently. That removes the
  recurring stale-ledger problem at the root rather than patching it.
- **Editions become genuinely disposable.** Either edition can be deleted
  and rebuilt from masters with no data loss. That is what "edition"
  should mean.

**Masters are never physically altered after finalize.** That is the line
between this tier and everything below it.

### Edition
A **rendering of the masters for a target**. Derived, disposable,
rebuildable. **Always physically altered.**

| Edition | Format | Loudness | Extras | Built by |
|---|---|---|---|---|
| **Lossless Edition** | ALAC in .m4a | **-18 LUFS** baked, -1.0 dBTP, 11.0 LRA | playlist | `scripts/alac_library/build_alac_library.py` |
| *lives at* | `/home/grey/Music` (1.8 TB partition, 647 GB free, ~1.1 TB once the 470 GB of old music clears) | | | |
| **Car Edition** | AAC 256k in .m4a | **-14 LUFS** baked | noise masking (Y/N), playlist | `scripts/car_library/build_car_library.py` |

Both read from `ALAC_Archive`. Neither reads from the other.

### Deliver
Putting an edition onto a device. Never builds, never converts -- it
moves what an edition already produced.

| Delivery | Target | Built by |
|---|---|---|
| **To USB** | wiped, reformatted **exFAT** for Android; throttled copy, quality over speed; asks for the device | `scripts/usb_transfer/transfer_to_usb.py` |

---

## Mapping from the notes

| Notes | Now called |
|---|---|
| (implicit, unnamed) | **Masters** — `ALAC_Archive` |
| Act 2A — ALAC_Library | **Lossless Edition** |
| Act 2B — ACC car_Library | **Car Edition** |
| Act 3 — USB transfer | **Deliver → To USB** |

The pipeline's Act 1 / Act 2 / Act 3 keep their names and mean only intake.

---

## What the code already does, and where it disagrees

`build_alac_library.py`'s docstring already describes exactly this shape:
it bakes **from** `ALAC_Archive` ("the pristine, unbaked tier") **into**
`ALAC-Library`, selecting `status='CATALOGUED' AND file_path under
archive_dir AND lufs_baked_at IS NULL`. So the two-tier design exists in
code; it was missing only from the vocabulary.

**But the tiers are not in that state today** (measured 2026-08-31):

| | files | size |
|---|---|---|
| `ALAC_Archive` (masters) | **7,517** | 297 GB |
| `ALAC-Library` | **10,635** | 457 GB |

`ALAC_Archive` is **3,118 files behind**. It is not yet the master tier in
fact, and populating it is a real migration step, not a rename.
`lufs_baked_at` is NULL on all 10,656 rows, so **nothing has ever been
baked** -- the split can be adopted cleanly rather than retrofitted.

---

## Storage — answered, and it changes the plan

Masters and editions no longer compete for the same volume:

| tier | volume | size | free |
|---|---|---|---|
| **Masters** | `/mnt/NUC8TB_BACKUP` | ~420 GB | **3.9 TB** |
| **Lossless Edition** | `/home/grey/Music` | ~420 GB | 647 GB now, ~1.1 TB after cleanup |
| **Car Edition** | TBD | much smaller (256k AAC) | — |

`/home` is its own 1.8 TB partition (`nvme0n1p4`); the OS sits on a
separate 28 GB `/`. A large edition on `/home/grey/Music` cannot threaten
the system volume. An earlier draft of this doc worried about that and
was wrong.

## "Rebuilding" masters is a MOVE, not a rebuild

`lufs_baked_at` is NULL on all 10,656 rows -- **nothing has ever been
baked**. So `ALAC-Library` today already *is* fully-corrected, unbaked
ALAC, which is exactly the definition of Masters above. It is in the
wrong place with the wrong name and nothing more.

Masters are therefore produced by **moving** `ALAC-Library` to
`/mnt/NUC8TB_BACKUP`. No transcoding, no baking, no re-derivation.

This inverts the direction `build_alac_library.py` assumes (it reads
`ALAC_Archive` -> writes `ALAC-Library`). The script's *mechanics* are
right; its two endpoints swap.

### The existing `ALAC_Archive/2026-08-27B` is stale and gets retired

Measured 2026-08-31, and it is not a candidate for the masters tier:

| | ALAC_Archive | ALAC-Library |
|---|---|---|
| files | 7,517 (one batch) | 10,557 + 39 |
| rows in `archive` pointing at it | **0** | 10,649 |
| `soar` sort tags | 303 | 2,062 |
| MusicBrainz IDs | **593 (7.9%)** | 9,944 (94%) |
| stale casing (`Abba`, `Tlc`) | 18 | **0** |

It is a four-day-old snapshot from a one-off migration script, unknown to
the database, missing every correction made since. Retired, not promoted.

## One collision this resolves

Two different things both claimed "car library":

- **`CuratorStage`** (`NAME = "curator"`, described in `cli.py` and
  `console.py` as "Build car-library export") -- **copies** files into an
  export tree and adds **companion noise tracks** as separate files
  (`Pink_Noise_30min.m4a`). No transcoding, no loudness change.
- **`build_car_library.py`** -- **encodes** to 256k AAC and **mixes noise
  underneath the audio**. This is the Car Edition.

Curator is not the Car Edition and should stop being described as one.

**Decision:** do not rename the stage class -- `curator` is its event
name, its CLI verb and the prefix on `car_export_path` -- fix the
*description* in the two places it misleads.

---

## Open, deliberately

- `car_export_path` and `noise_profile` are Curator's columns, named
  before this distinction existed. Left alone; renaming is a migration
  for no functional gain.
- ~~A third edition (phone, hi-res) would be another row in the Edition
  table, not a new scheme. The vocabulary is chosen so that holds.~~
  **Tested and held, 2026-09-16.** `iPHONE_Library` was added — AAC 256k,
  −14 LUFS, 5,693 files — and needed no new vocabulary, no new table and no
  change to this document. It is an Edition; the iPhone is a Delivery; the
  files it carries are not masters and nothing downstream treats them as
  such. The one thing the phone did add is a *delivery* constraint this
  document did not anticipate: iOS sandboxes each app, so a file placed in
  VLC's container can never be seen by the Music app. That is a property of
  the Delivery, not of the Edition, and it is recorded where it belongs — in
  `scripts/iphone_transfer.py`, which prints the `ifuse` commands for the
  operator and runs none of them.


---

## Should moving a file between folders TRIGGER an edition?

**Proposed (Grey):** raw files -> INBOX -> land in `ALAC_Archive`; the user
physically moves what they want into `ALAC-Library` for LUFS baking to
happen; a third folder `Car_Library` for the car edition.

**The intent is right and is kept:** building an edition must be an
explicit, deliberate act. It bakes loudness irreversibly into audio and
should never happen because a batch finished.

**The mechanism should not be a file move.** Three reasons, all concrete
to this codebase:

1. **Other stages already move files.** `FinalizeStage` moves INBOX ->
   library; `OrganizeStage` moves within a batch and is now wired into
   Act 3. If "a file appearing in `ALAC-Library` means bake it", then
   Finalize triggers baking as a side effect of finishing, which is the
   opposite of deliberate.
2. **A half-copied file is indistinguishable from a finished one.** A
   420 GB move takes time; a directory scan cannot tell "still copying"
   from "ready". The DB can -- that is what `finalized_at` is for.
3. **The database already answers "which files".** Selection by status,
   batch, artist or playlist is a query. Re-expressing it as a physical
   layout means maintaining the same information twice, which is how the
   `hash_index` / `archive` divergence started.

**Instead:** the console asks. `Build an Edition -> Lossless Edition ->
[all masters | one batch | a playlist | a saved selection]`, dry-run
first, showing counts and estimated size before it writes anything. The
folders stay *outputs*, never triggers.

That keeps the deliberateness Grey is asking for -- nothing bakes unless
a human picks it from a menu -- without making the filesystem the control
plane.

**Car Edition** gets its own root for the same reason the others do, and
is named in the same scheme rather than as `Car_Library`. Location TBD;
it is far smaller (256k AAC) and is the least constrained of the three.
