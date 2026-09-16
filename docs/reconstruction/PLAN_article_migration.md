# Plan — move the article to where the world expects it

**Written 2026-09-16. Not started. Grey's sequencing: this runs LAST or
second-to-last, with the ultrareview as the other candidate for that slot.**

---

## The problem, stated once

MUSAEUS stores `Beatles, The` in `archive.artist`. That is the **sort form**,
and it is the right thing for a folder-browsed library — it files under B.

But `archive.artist` is also what every external lookup uses, and **no music
service has ever heard of `Beatles, The`**. `musaeus/artist_form.py` measured
this on 2026-08-29: *376 of 839 cached misses were in `X, The` form, and 0 of
2,158 hits were.*

It has since broken four separate things, each found independently:

| where | what happened |
|---|---|
| Source lookups | 1,072 proposal rows in `X, The` form, **1,071 returned nothing** (99.9%) |
| MusicBrainz | its quoted phrase search is exact; the article form returns 0 recordings |
| MasterLaw checks | a naive membership test misses — `GenreLaw._key()` folds it, ad-hoc code does not |
| CAR match-back | `(artist, title)` exact match failed for **2,359 tracks** |

Each was fixed where it was found. That is four patches for one cause, and
the next place will break too.

## The fix already exists

`musaeus/artist_form.py` says it plainly — three fields, three jobs:

```
artist (©ART)   "The Stooges"     natural form -- what MusicBrainz, Plex
                                  and every player expect
soar            "Stooges, The"    sort form -- what players sort by; this
                                  is exactly what the tag is for
folder          "Stooges, The"    sorted browsing on disk, unchanged
```

**The module is written. The library has never been migrated onto it.**

## Measured scope, 2026-09-16

| | |
|---|---|
| CATALOGUED rows | 11,490 |
| rows with `artist` in sort form | **1,915** |
| distinct artists affected | **397** |
| `natural_form` → `sort_form` round-trip failures | **0 of 1,915** |
| `MasterLaw.csv` rows in sort form | 0 — `GenreLaw._key()` already folds |
| `artist_canon.tsv` rows in sort form | **4** |
| `artist_filing.tsv` rows in sort form | 0 |

**Zero round-trip failures is the load-bearing fact.** Every one of the 1,915
converts to natural form and back to exactly the stored string. The migration
is provably reversible per row, not merely believed to be.

It is also smaller than it felt: 397 artists, and the authorities need a
4-row sweep rather than a rewrite.

## Order of work

**1. Widen the guard first, before anything moves.**
`doctor` gains a check: any `archive.artist` in sort form is a finding. Red
before the migration, green after, and it stays as the thing that stops the
convention drifting back. A migration with no standing check is a cleanup
that has to be repeated.

**2. Write the `soar` tag, leave `artist` alone.**
Nothing breaks: players that sort by `soar` start sorting correctly, and
`artist` still holds what it always did. Reversible by deleting a tag. Prove
sorting is right in a player before touching `artist` at all.

**3. Flip `archive.artist` to natural form.**
1,915 rows, in one transaction, with the DB backed up first. Log every row as
`ARTIST_FORM_MIGRATED` with the old value, so the event log alone can rebuild
the previous state.

**4. Re-tag the files.**
`©ART` becomes natural form, `soar` keeps sort form. Per-file, verified by
re-reading the tag, and a file that fails to write leaves its row untouched
so a re-run retries it. **Do NOT move or rename anything.**

**5. Folders stay exactly as they are.**
`artist_filing.tsv` already answers "what is the folder called" separately
from "what does the tag say". That separation exists for this. Folders remain
`Beatles, The`; nothing on disk moves.

**6. Sweep the 4 `artist_canon.tsv` rows.**
Any bulk rename of `archive.artist` must sweep the authorities in the SAME
change — the 2026-09-06 rename left 38 MasterLaw keys stranded and those
rules went dormant, which looks exactly like a rule that was never written.

**7. Re-run the guard, then the lookups.**
`doctor` green. Then re-run the album-name pass over whatever still has no
album: the 397 artists that could never be looked up become lookupable for
the first time.

## What could go wrong, and what stops it

| risk | control |
|---|---|
| A name that is not an article form gets mangled | `PROTECTED_ARTIST_NAMES` — this rule has regressed three times, including splitting **De La Soul** into "La Soul, De". Round-trip test on all 1,915 before writing anything. |
| Tag write succeeds, DB write fails, or the reverse | Disk first, then DB, per row — the SOP §4.12 order already used by the bake. A crash leaves a retryable row, never a lying one. |
| Sorting breaks in a player | Step 2 ships `soar` alone and is verified in a real player before step 3 |
| Folders drift from tags | Folders are deliberately untouched; `artist_filing.tsv` is the authority for them |
| The convention creeps back | The `doctor` check from step 1 stays forever |

## Exit proof

- `doctor` reports 0 rows in sort form
- all 1,915 files carry natural `©ART` **and** sort-form `soar`, verified by
  re-reading the tags, not by trusting the write
- no folder has moved: the on-disk tree is byte-identical in shape
- a spot-check of 20 previously-unlookupable artists now returns album data
- full suite green

## Deliberately NOT in scope

- renaming folders
- touching `/home/grey/Music`
- changing `GenreLaw._key()`, which already folds articles correctly
- the 827 tracks with no CAR file — a separate gap
