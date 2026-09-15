# MUSAEUS — Wishlist

**Created 2026-09-07.** Split out of MUSAEUS_TODO.md on Grey's instruction.

The distinction is deliberate and worth keeping:

> **TODO** protects what exists. **WISHLIST** makes the library bigger or
> better-chosen.

Nothing here is broken. Nothing here has a deadline. Everything here is
genuinely wanted — that is why it is a wishlist and not the
"deliberately not doing" list, which is a different thing again and lives at
the end of MUSAEUS_TODO.md with the measurements that settled each one.

Ordered by Grey's own priority call, 2026-09-07.

---

## 1. Genre gaps from an external source — **BUILT 2026-09-07**

Moved here only to record that it is done: `scripts/itunes_genre_fill.py`,
commit `0394814`. Asks the iTunes Search API what genre an artist is, for
artists no authority has an opinion about, and writes high-confidence answers
to `MasterLaw.csv`.

Kept at the top because it is the one whose absence was actually paid for:
41 artists ruled on by hand over 2026-09-06/07.

Two rules learned on its first live run and now enforced:
- **ask the library before the API** — where an artist's own tracks already
  agree on a genre, that settles it
- **rare genres go to review, never to the law** — iTunes returned "Holiday"
  for a Blues Brothers covers act

---

## 2. FM-radio version identification

**What it does.** For a song held in several versions, work out which one is
*the version you remember from the radio* — first pressing, right year,
standard single length rather than the 7-minute album cut or a 2011 remaster.
ORPHEUS had `orpheus_fm_radio_identifier.py`; it queries MusicBrainz for all
pressings and flags the likely original.

**Why it is wanted.** It is not a new capability so much as the automation of
a judgement Grey has already made by hand a dozen times — Jan & Dean's
instrumental version, the Everly Brothers' single-version-versus-2006-
remaster, the "(2011 Remaster)" calls. MUSAEUS already picks a keeper on
codec, bitrate and reissue status; this adds *"and prefer the pressing that
actually charted"* as one more tiebreaker in `_keeper_sort_key`.

**What to watch.** It needs MusicBrainz, so it cannot run offline. "Likely FM
radio version" is a heuristic and will be wrong on jazz, classical, and album
cuts that were never singles. **It should propose into a CSV, never decide** —
the same shape as every other review artefact.

---

## 3. Discography gap detection

**What it does.** Compare the library against MusicBrainz canonical
discographies and report albums you own *nothing* from. Not "you are missing
track 7" but "you own five Springsteen records and *Nebraska* is not one".
ORPHEUS had `orpheus_discography_diff.py`.

**Why it is wanted.** It fills holes you do not know you have, and its output
feeds `TuneMyMusic.csv` the same way the re-sourcing entries already do.

**What to watch.** A canonical discography includes live albums,
compilations, regional pressings and reissues, so raw output is noisy and
needs a scope ruling before it is useful. ORPHEUS cached results, which tells
you it was slow.

---

## 4. Deliberately excluded from this list

**Prowlarr / Lidarr acquisition search** — dropped on Grey's instruction,
2026-09-07.

**Lossless-fraud detection** — this is not a wish, it is a rejection. Built
and measured 2026-09-07: 80% overlap between known-lossy and known-lossless
sources, every threshold mislabelling as many real files as it catches.
It belongs in MUSAEUS_TODO.md's "deliberately not doing" section with the
data, and it is recorded there.

---

## A note on the split

TODO items have a cost if left undone — something is unprotected, or a check
is lying. Wishlist items have no such cost; the library is correct without
them.

If you are ever choosing between the two lists with limited time, take the
TODO. A bigger library that cannot be trusted is worse than a smaller one
that can.
