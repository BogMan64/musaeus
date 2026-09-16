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

---

# OUTCOME — executed 2026-09-16

Grey re-sequenced this to run FIRST rather than last: *"i thought we were
going to 'fix' the article for MB easy read... If we are, lets do that
first."*

## What was done

| step | outcome |
|---|---|
| 1. Guard first | `doctor._artist_tag_is_natural_form` added. Proved RED (396 artists / 1,914 tracks), then green. |
| 2. Write `soar` | **NOT DONE — see below.** |
| 3. Flip `archive.artist` | 1,914 rows converted, logged `ARTIST_FORM_MIGRATED` with old and new value. DB backed up to `_db_backups/musaeus_pre_article_migration_20260916T113533Z.db` first. |
| 4. Re-tag the files | **Not needed.** The files already carried natural form — sampled 40 before, all 40 natural, 0 sort form. The divergence was DB-only. |
| 5. Folders untouched | Held. Nothing on disk moved. |
| 6. Sweep the authorities | 3 canonical values swept in `artist_canon.tsv`. Raw keys deliberately left in sort form — they are match keys for dirty input. |
| 7. Re-run the guard | Green: "every artist is in natural form". |

## Step 2 was dropped, deliberately

The plan sequenced `soar` first so sorting could be proved in a real player
before `artist` moved. That rationale evaporated at step 4: the files already
held natural form in `©ART`, so players were ALREADY sorting by the natural
form and had been all along. Writing `soar` would have changed player sort
order — the one thing step 2 existed to protect — rather than preserving it.

**No file was re-tagged, so no `soar` atom was written.** A future session
checking the old exit proof ("all 1,915 files carry sort-form `soar`") will
find none, and that is correct, not a failed migration. Writing `soar`
remains available as a separate, optional improvement.

## The durability defect, found after the migration reported success

`NormalizeStage` ran `UPDATE archive SET artist=?` with
`_normalise_artist()`, whose last act was `_move_article_to_suffix`. The
migration was therefore **cosmetic**: the next normalize run would have put
all 1,914 rows back, one at a time, with doctor going quietly red days later
and nobody having changed anything.

`_normalise_artist` now returns `natural_form(_move_article_to_suffix(name))`.
The suffix pass still runs — it is what folds the junk spellings
("Archies (the)", "Beatles, The (the)") into one shape and what honours
`PROTECTED_ARTIST_NAMES` — but the article is put back in front before the
value is stored. Normalize now *enforces* the convention instead of undoing
it: `'Beatles, The'` → `'The Beatles'`.

Sort form still exists in the two places that want it, both deriving it
themselves and unaffected: the folder path (`organize.py`) and the `soar`
tag (`tagger.py`).

## The guard used the wrong predicate at first

Written with `has_article()`, which is a correct function asked the wrong
question — its docstring says "True when the two forms differ -- i.e. the
name carries an article", and "The Beatles" carries one as surely as
"Beatles, The" does. The guard therefore reported all 1,914 rows still in
sort form in the same breath as the migration reporting all 1,914 converted.
Both were true about different questions.

The sort-form test is `a.strip() != natural_form(a)`. Pinned by
`tests/test_doctor_artist_tag_form.py`, including a test that fails if the
guard ever imports `has_article` again.

## Exit proof, as measured

- `doctor`: **green** — "every artist is in natural form", count 0.
- Non-CATALOGUED rows checked too: 0 in sort form under any status, so the
  guard's `status='CATALOGUED'` clause is not hiding a future red.
- All 396 distinct conversions reviewed by hand; the 14 non-English ones
  (`Bravos, Los` → `Los Bravos`, `Dannan, De` → `De Dannan`,
  `Gran Combo, El` → `El Gran Combo`) are all correct. **Round-tripping was
  not accepted as proof** — a wrongly-split name round-trips cleanly, which
  is exactly how "De La Soul" became "La Soul, De" three times.
- 20 files sampled: `©ART` matches the migrated DB value in 20 of 20.
- **Lookups measured before and after, which is the point of the whole
  exercise.** MusicBrainz, whose query is the exact phrase
  `artist:"NAME" AND recording:"TITLE"`:

  | title | sort form | natural form |
  |---|---|---|
  | Start Me Up | `Rolling Stones, The` → **0** | `The Rolling Stones` → **491** |
  | All I Have to Do Is Dream | `Everly Brothers, The` → **0** | `The Everly Brothers` → **387** |
  | The Whole of the Moon | `Waterboys, The` → **0** | `The Waterboys` → **75** |
  | Cherry Bomb | `Runaways, The` → **0** | `The Runaways` → **20** |

  **8 of 10 went from 0 hits to hits.** The 2 that did not are a
  collaboration credit and a title mismatch — neither an article problem.

## A correction to this plan's own premise

The plan implied every source was blind to the sort form. **iTunes was not.**
Measured on 10 migrated artists, iTunes returned a hit for the sort form and
the natural form equally — its search is fuzzy and always coped.

The damage was concentrated in **MusicBrainz**, whose quoted phrase search is
exact. The 2026-08-29 measurement quoted at the top of this plan (376 of 839
cached misses in `X, The` form) is consistent with that: it measured the
cache across sources, and MusicBrainz was the source failing.

This does not weaken the case for the migration — MusicBrainz is the
structural authority for release-group type, which is how compilations get
filtered — but "no music service has ever heard of `Beatles, The`" was too
broad, and is corrected here rather than left to mislead the next session.
