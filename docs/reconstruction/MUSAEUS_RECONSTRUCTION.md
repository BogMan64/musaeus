# MUSAEUS — Reconstruction Document

**Written 2026-09-07.** Sections 3, 4 and 5 of a planned seven. These three
first because they hold what nothing else holds.

**The code is recoverable. The judgement is not.** If the repository
survives, nobody needs prose describing `CorruptStage` — they can read it.
What cannot be recovered from any surviving artefact is *why*: why `&` joins
artist names but `and` joins song titles, why a 45-second floor must not be
raised to 120, why tribute-quarantine has to stay broad, why a cover is kept
if it charted for whoever released it. Every one of those took a measurement
or an owner's ruling to settle, and every one would be re-litigated — wrongly
— by a future reader working from the code alone.

Sections 1, 2, 6 and 7 (what MUSAEUS is, the data model, the pipeline,
deliberately-not-doing) are still to write. All figures below were verified
against the live vault on 2026-09-07.

---

## 3. The authorities

MUSAEUS keeps **six** separate stores of truth. Each is internally
consistent. None is aware of the others. This is the single most important
structural fact about the system, and nearly every serious bug found in its
history has been two of them disagreeing.

| authority | location | governs | size |
|---|---|---|---|
| `MasterLaw.csv` | `VAULT/MetaData/` | artist → genre | 3,472 rows |
| `artist_canon.tsv` | `VAULT/MetaData/` | raw artist name → canonical name | 269 entries |
| `Genre_Allowed.txt` | `VAULT/MetaData/` | the closed genre vocabulary | 49 genres |
| `Genre_Canonical_Map.txt` | `VAULT/MetaData/` | raw genre → canonical genre | 207 mappings |
| `denied_hashes` | `VAULT/_db_backups/hash_index.db` | audio refused re-ingest | 19 entries |
| `protected_artists.py` | `musaeus/canon/` | names that must never be split | 13 names |

Two more things behave as authorities without being files:

- **the `archive` table** in `VAULT/musaeus.db` — the library itself
- **the folder names on disk**, which are derived from `archive.artist` but
  drift from it, and which sometimes hold the *correct* styling while the
  database holds a damaged one (`k.d. lang` on disk, `K.d. Lang` in the row)

### The standing rule

**A single authority can be correct and the system still wrong.** Grey named
this himself on 2026-09-05: *"You keep being right and the code keeps only
half-hearing you."*

When the owner says "I thought I settled this", assume he did — then find
*which* authority holds the ruling and *which* never learned it. That has
been the answer every single time.

### What each one is for, and its traps

**`MasterLaw.csv` — artist → genre.** The law. Two columns, no header.
Lookup goes through `GenreLaw._key()`, which folds case, whitespace and the
article convention (`The Byrds` / `Byrds, The` / `Byrds (the)` all key to
`byrds`). It does **not** fold `&` versus `and`, and that asymmetry has bitten
twice — see §5.

**`artist_canon.tsv` — raw name → canonical name.** Tab-separated. Both sides
matter: a name appearing only as a rename *target* is still an artist someone
has ruled on. `resolve_exact()` does not follow chains, so an entry pointing
at a name that is itself a key leaves a row stranded half-way.

**`Genre_Allowed.txt` — the closed vocabulary.** 49 entries, one per line,
`#` comments. **Split on newlines only**: `Pop, Rock` was one genre whose name
contained a comma, and `R&B/Funk/Soul`, `Disco/Electronic` and `Rock N'Roll`
contain separators too. Nothing in the library may hold a genre absent from
this file, and `doctor`'s *authorities agree* check enforces the reverse —
no MasterLaw genre may be absent from it either.

**`Genre_Canonical_Map.txt` — raw → canonical.** `raw => canonical` per line.
**A target here can be stale**: `Baroque` was still a target after the genre
was retired from the vocabulary on 2026-09-07. Always validate a mapped
result against the *current* `Genre_Allowed.txt`, never against the map.

**`denied_hashes` — audio refused re-ingest.** Keyed on the **PCM audio
hash**, so it survives re-tagging and container rewriting, and so it blocks
*this recording* and not *this song*. A different rip of the same song hashes
differently and gets through. That distinction matters when explaining why
something reappeared.

A reason string is not decoration. `deny_list._is_advisory()` matches one
exact phrase — the 2026-08-24 backfill wording — and treats anything else as
**binding**. An advisory entry is noted and let through; a binding one
refuses. Write the reason deliberately.

**`protected_artists.py` — never split these.** 13 names whose commas or
ampersands are part of the name (`Earth, Wind & Fire`, `Crosby, Stills & Nash`,
`Of Monsters and Men`). This file exists because **four copies of the same
list had diverged** across the codebase, holding 12, 9, 6 and 6 names.
Anything that rewrites `archive.artist` must consult it first.

`tribute_quarantine.py` keeps a *separate* protected list for a different
purpose — real artists who must never be quarantined even when a title or
album matches a junk pattern (Bruce Springsteen performing at a Jackie Wilson
tribute; Spirit, whose own song is called *Tribute To The Rain Woods*). Do
not merge the two lists: they answer different questions.

---

## 4. Conventions, and the rulings behind them

Every entry below is an owner's decision. Several look arbitrary and are not;
the reasoning is what makes them reconstructable.

### Names

**`&` joins artist and artist-group names. `and` belongs in song titles.**
Applied library-wide 2026-09-06: 167 artist rows `and` → `&`, 369 title rows
`&` → `and`.

Two things make this safe, and both are load-bearing:

1. **Only a *spaced* ampersand is touched.** `S&M`, `G&B` and `R&B` survive
   because ` & ` is the target, not `&`.
2. **A bulk rename of `archive.artist` must sweep MasterLaw and
   `artist_canon.tsv` in the same change.** The 2026-09-06 rename left 38 law
   keys on the old spelling, and since `_key()` does not fold `&`/`and`, those
   38 rules went dormant. **A dormant rule looks exactly like an absent one.**

The original reason for the convention — helping MUSAEUS tell an artist from
a title — no longer applies. The convention stands anyway, for consistency.

**The article convention: `Beatles, The`, not `The Beatles`.** Inherited from
ORPHEUS so a folder listing sorts under B. The *tag* on disk carries the
natural form (`The Beatles`); `soar`/`soaa` carry the sort form. Three fields,
three jobs — see `musaeus/artist_form.py`. Measured 2026-08-29: of 839 cached
MusicBrainz misses, 376 were in `X, The` form and **0 of 2,158 hits were**.
Not one article-suffix lookup had ever succeeded.

**Classical is filed by composer, not performer.** Grey, 2026-09-06.
`Elly Ameling` and `English Baroque Soloists` performing Handel are filed
under **Handel**.

**A collaboration credit resolves to the lead artist.** `Benny Goodman & His
Orchestra` → `Benny Goodman`; `Louis Armstrong & His Hot Five` → `Louis
Armstrong`. Applied through `artist_canon.tsv`, never by string surgery — the
protected list exists precisely because splitting on `&` destroys real names.

### Genres

**One genre per artist**, never per song. Grey's rule, 2026-08-21: a library
where Bob Dylan is 84 tracks of Rock and 24 of Folk Rock is not richer for
the distinction, it is harder to browse. `doctor` enforces it.

**The vocabulary is closed.** A genre not in `Genre_Allowed.txt` is not a
genre. Admitting one is a deliberate act with a dated comment saying who
ruled and why.

**Retiring a genre is a five-place sweep.** `Rock & Roll` → `Rock N'Roll` on
2026-09-06 touched: the vocabulary (1 line), MasterLaw (122 rows), the
library (854 rows), the canonical map (7 targets, plus a new
`Rock & Roll => Rock N'Roll` so existing tags resolve forward) — **and a
hardcoded tuple in `musaeus/editions.py`** that no check reads. That fifth
copy would not have dropped a track; it would have quietly demoted the
owner's third-priority genre to alphabetical order on a budget-constrained
transfer. **Grep the code before assuming a rename is data-only.**

### What belongs in the library

**Only true recording artists.** Karaoke, tribute and cover *products* are
removal candidates. A cover **by a real artist** is music and stays.

**The cover rule, settled 2026-09-03 and deliberately never automated:** a
cover is in if it is the artist's own work, **or** if it charted for whoever
released it. Springsteen's own *Blinded by the Light* never charted; Manfred
Mann's went to #1. Both stay. **A mechanical rule destroys the first**, which
is exactly why this stays a per-record judgement. Jazz and blues standards
are exempt by design — take the best copy.

**A chart-based knock-off rule was considered and rejected** (2026-09-07).
"Did it chart" needs external data MUSAEUS cannot verify offline, and would
delete session players, B-sides, album tracks and most of the jazz and blues.
The cheaper signal that *does* work is **an artist no authority has ever
ruled on** — see `scripts/artists_to_review.py`.

**Delete means delete.** The owner chooses permanent removal over reversible
quarantine, every time. What makes that safe is not a quarantine folder but
the event log: every deletion records the reason and the `audio_hash`.

**A deletion ruling must reach `denied_hashes` or it is not permanent.**
On 2026-09-03 the owner authorised permanent deletion of a track; the hash
was never added to the deny list and the audio was re-ingested the next day.

### Editions

**Three tiers, and the direction of travel is one-way.**

| tier | what it is | current |
|---|---|---|
| `ALAC_Archive` | pristine masters, never baked | 15,820 files, 592 GB |
| `ALAC-Library` | the −18 LUFS baked edition | 14,855 files, 613 GB |
| `CAR_Library` | the AAC edition, −14 LUFS | 10,036 files, 72 GB |

**Masters are never baked. Each edition bakes exactly once, from the masters.
No edition is ever built from another.** An edition is derived and
disposable; a master is not. Parking the only copy of anything inside an
edition loses it on the next rebuild.

A baked row's `file_path` follows the *edition*, so an ALAC_Archive master
whose library copy exists carries no row of its own. That is by design and is
why `doctor`'s orphan scan covers `alac_library` only.

**`TuneMyMusic.csv`** (`ALAC_Archive/`, `Title,Artist,Album`) is the
re-sourcing list — what to re-acquire. Add to it when deleting something
**loses the song**. Do *not* add when the owner rejected the *artist*: queueing
"Juice / Lizzo" for re-download after he removed Lizzo undoes his own ruling.
And do not add when another copy survives — he had three such lines removed
on 2026-09-06.

---

## 5. The failure catalogue

Every entry is a real defect found in this system, with the guard that now
prevents it. **This is the section most worth reading before changing
anything**, because each one looked impossible until it happened.

### The shape they all share

**Two places holding the same fact, disagreeing.** Five instances were found
on 2026-09-06 alone. If you remember one thing from this document, remember
to go looking for the second copy.

### Green checks that were wrong

**`library files with no row: 0` beside 19 GB it could not see.** The scan
covers `alac_library` and excludes the review folders — but both review
folders live under `alac_archive`, which it never visits at all. 484
untracked files, 19 GB, named `Unknown Artist - Unknown Title (N).m4a` while
their internal tags were perfectly correct.
**Guard:** a reciprocal check, *review folders with no row*. It found 7
stranded files on its first run.
**Lesson:** a ✓ is read as "nothing is unaccounted for". When a check says
everything is fine, ask what it actually looked at.

**A verification that had been red so long it read as normal.** `tagger`'s
`verify_effect` compared the natural-form artist tag against the row's *sort*
form, so every article artist reported as unverified — 3,161 files, every
run, on a library that was correct on disk.
**Lesson:** a permanently-red check is a check nobody reads.

### Checks that accused the wrong thing

**A whole-library decode audit reported three undamaged files as damaged.**
`ffmpeg -i file -f null -` decodes *every* stream in the container, and an
.m4a carries its cover art as a second, video stream. Three files with a
malformed embedded JPEG — Andy Gibb, Baltimora, Chamillionaire — decoded
their full ALAC stream with exit 0 and were still called damaged, because
the verdict was `if proc.stderr.strip(): return False`. The artwork's
decoder had written to stderr; nothing asked which stream it was about.

Neither `-vn` nor `-map 0:a` suppresses those lines on its own: the mjpeg
header is parsed at demux time, before stream selection applies, so plain
`ffprobe` prints them too. The fix needs both a narrower command and a
classifier.

**Why it had never been seen.** In `DEFAULT_PIPELINE` the order is
`corrupt → albumart`. CorruptStage decode-checks files *before* artwork is
embedded, so the files it sees have no image stream. The same defect sat in
`ffmpeg_decode_check` the whole time and could not fire. It surfaced the
moment `scripts/decode_audit.py` became the first thing to decode the
**finished** library.

**Guard:** `audio_relevant_stderr()` in `musaeus/stages/corrupt.py`, with
`-vn` on the command, and `decode_audit.py` now *delegates* to that one
function rather than carrying its own copy — the two copies were what let
them disagree in the first place. A **third** copy was found the same day in
`musaeus/duration.py::decodes_cleanly()` and now delegates too. A **fourth**
exists in `scripts/car_library/vendor/orpheus_noise_generator.py` and is
deliberately left alone: it only ever checks the six noise beds the
generator creates itself, which carry no artwork, so the defect cannot fire
there. That is a reason, not an oversight — do not "fix" it without one. Vacated verdicts are corrected by a
`DECODE_VERDICT_VACATED` event, never by deleting the original.
**Lesson, and it is the one that generalises:** the classifier is a
**denylist** — drop lines known to come from an image decoder, keep
everything else. An unrecognised image codec then leaks through as a false
positive, lands in the review CSV, and a human rules on it. An allowlist of
known audio errors would fail the other way: an unrecognised audio error
would be dropped and a damaged master would be baked into an edition.
**Fail towards the human, not towards the encoder.**

### Checks that went quiet instead of red

**`bitrot` reported nothing because it was comparing nothing.** The
integrity check for `ALAC_Archive` — the tier that is supposed to stay
byte-identical for ever — ran on 2026-09-08 and returned:

```
files to verify: 15816
ok: 0
corrupt (hash mismatch): 0
new (no baseline yet): 15816
missing from disk (was baselined, gone now): 1385
```

Zero corrupt, and a green tick. Every baselined path was gone; every file
present was unrecognised. The check had 0% coverage and announced it only as
a large number beside a pass.

**Cause:** `archive_tier_hashes` keys on **path**, and `organize`,
`canonicalize`, `finalize` and the LUFS bake all move or rename files as
ordinary business. Each move orphans a baseline row. The failure mode is the
dangerous direction — an orphaned row makes the check report *new*, which is
benign-sounding, rather than *changed*, which is not.

**Guard:** the baseline now also records `audio_hash`, the PCM identity,
which survives both a move and a re-tag. A file not found at its baselined
path is looked up by that and reported as **moved**; a byte change with an
unchanged PCM identity is reported as **re-tagged**, not rot; a byte change
with no baselined identity is **unclassifiable** and still fails. And a
verify whose corpus is more than half unbaselined now reports **failure**,
because it did not verify anything.

**Lesson:** this is the same defect as `library files with no row: 0` beside
19 GB, and it will keep recurring in this shape. **A check that finds
nothing is not the same as a check that found nothing wrong.** When a result
is green, look at what it says about its own coverage before believing it —
and if the check does not report its coverage, that is the first thing to
fix.

### Rules that fought each other

**`tagger` rewrote 3,161 files on every run, forever.** Two rules owned the
`artist` field: a generic field-map wrote the row's sort form, a dedicated
block wrote the natural form. For a file that was **already correct** the
generic test was true and the specific one false, so the sort form won and
clobbered a good tag; the next run put it back. Four consecutive runs
reported 3,997 / 3,161 / 3,161 / 3,161 changes.
The churn was the symptom. The cost was that on any run ending with the sort
form written, those files carried the one spelling that has never matched
MusicBrainz.
**Guard:** `artist` removed from the generic map; a test feeds the computed
changes back in and requires the second pass to ask for nothing.
**Lesson: run every stage twice.** A stage that has finished reports zero.
Nothing else would have found this.

### Silent absence

**Four CLI commands raised `ModuleNotFoundError` for a month.** Commit
`bf948fc`, titled *"feat: Add idle-aware LUFS forge utility script"*, added
two files and **deleted six**. Four were imported by `cli.py` — inside the
command functions, so nothing failed at startup and no test touched them.
`musaeus report`, `spec-scout`, `upgrade-check` and `canon-review` all
crashed on invocation while `--help` kept listing them.
Found by a second pair of eyes reading the tree, not by anything in the repo.
**Guard:** `tests/test_cli_script_imports_resolve.py` walks the AST of every
module, finds each `from scripts.X import`, and asserts the target exists.
**Lesson: `--help` is a promise, and nothing was checking it.**

### Thresholds that cannot work

**An absolute duration floor cannot tell a fragment from a short song.**
ORPHEUS used `duration < 1.0s`; it caught a 0-second file and missed two at
exactly 1 second. Raising the floor is worse: measured on the live library, a
**120-second floor flags 397 complete recordings** — *Hit the Road Jack*
(2:00), *All Shook Up* (1:58), *It's Not Unusual* (2:00). Early rock and 60s
pop are supposed to be short.
**Guard:** compare a file against *another copy of the same recording*. Under
60s with a sibling past 120s is a fragment; 33-second *Bookends Theme* beside
an 83-second copy is not. Caught 6 of 11 before, 11 of 11 after.

**Spectral cutoff cannot detect a lossy transcode in this library.** Built
and measured 2026-09-07 on 20 known-lossy and 20 known-lossless sources:
**80% overlap**, and every threshold mislabels at least as many genuine
lossless files as it catches lossy ones. Modern AAC at 256 kbps reaches
20–21 kHz; genuine lossless transfers of 1950s–70s masters are band-limited
by the *tape* — The Who's *The Kids Are Alright* tops out at 14.7 kHz, one
FLAC at 9.9 kHz. **Do not rebuild this.** Shipping it would flag the oldest
and most valuable recordings as frauds.

### Breadth that must not be narrowed

**`doctor`'s "removed audio still held" is deliberately broad**, and two of
its three hits at one point were benign. Narrowing it by reason would have
suppressed the one real defect in the same batch — a track whose permanent
deletion was authorised and which was re-ingested a day later. The fix was to
**rename the check and print each row's removal reason**, not to narrow it.
**Lesson:** when a check cries wolf, make it explain itself before you make
it quieter.

### Operational traps

**`pgrep -f <pattern>` matches your own command line.** It caught the same
worker four times in one session, twice killing its own shell. Use `pgrep -x`,
a PID, or put the pattern in a file executed separately.

**A review CSV listing one path in two groups makes contradictory rulings
possible.** A 170-group CSV listed 57 paths twice; 23 carried both DELETE and
KEEP; the apply honoured both in sequence and reported **zero errors** while
leaving 6 catalogued rows pointing at files it had just deleted.
The pipeline was never wrong — `dupe_resolver._connected_groups` merges such
groups. The CSV bypassed it.
**Guard:** the invariant is now asserted directly in a test so anyone building
a review artefact can find the function they must route through.

**An apply script that reports 0 errors while doing nothing.** After every
destructive operation, assert the post-condition: if rows-changed ≠
files-landed, fail loudly.

**Timestamps: the pipeline logs UTC.** Ad-hoc scripts using
`datetime.now()` write local time and land hours out of order in the event
log.

---

*Sections 1, 2, 6 and 7 remain to write. The event log
(894,275 events, 95 distinct types) is the audit trail of record and can
reconstruct much of what this document asserts, should it disagree.*
