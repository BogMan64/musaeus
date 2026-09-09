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

**Sections 1, 2, 6 and 7 added 2026-09-08**, from source only — no database
was opened, because a bit-rot rebaseline held the live one at the time. Where
a figure comes from the 2026-09-07 measurement pass it is marked; where it
comes from code it carries a `file:line`. Cross-checked against
`musaeus doctor` on 2026-09-08; where the two disagreed, `doctor` won.

> **The working copy of this document is
> `docs/reconstruction/MUSAEUS_RECONSTRUCTION.md`, in the repository. Edit that
> one and commit.** `~/Desktop/MUSAEUS_RECONSTRUCTION.md` is a published
> artefact refreshed from it, and is not version-controlled.
>
> This convention exists because it was learned the hard way on 2026-09-08:
> two sessions edited the Desktop copy within hours of each other, and the
> loser's work disappeared with nothing to show that it had. In the repository
> a collision is a merge conflict git puts in front of you; on the Desktop it
> is an edit that silently never happened. Do not edit the Desktop file.

---

## 1. What MUSAEUS is

**One person's music library, managed by software that is not allowed to
decide anything important.** That sentence is the whole design. Everything
awkward about this system follows from it, and every attempt to make the
software cleverer has had to be walked back.

MUSAEUS is a local-first pipeline for a single ~1.3 TB personal library across
three editions, run from one command on one Debian machine. It ingests audio,
hashes it by *content* so that re-tagging does not change identity, reads
metadata, checks for damage, resolves duplicates, measures loudness, writes
ReplayGain tags, files everything into Artist/Album, and exports a
car-stereo edition. It is a **clean-room successor to ORPHEUS**, and the name
is the point: Musaeus was the student of Orpheus, the one who wrote the
master's knowledge down.

Current state, measured 2026-09-08:

| | 2026-09-08 | 2026-09-07 |
|---|---|---|
| catalogued | **16,103** | 16,138 |
| deleted | **10,994** | 10,959 |
| ghost (row without file) | 3,208 | 3,208 |
| held for duplicate review | 51 | 51 |
| pending | 1 | 1 |
| event log | — | 894,275 rows, 95 distinct types |

Both columns are kept rather than the older being overwritten, because the
*movement* is the informative part: 35 rows left `CATALOGUED` for `DELETED`
in a day, and a reader who sees only one column cannot tell a stable library
from a busy one.

**The library has been decoded end to end.** On 2026-09-08 every one of the
16,107 then-catalogued files was decoded in full — not sampled, not
header-checked — and **8 were damaged: 0.05%**. Four had a clean copy of the
same recording and were deleted; four did not and are on the wanted list.
This is the strongest single statement that can be made about this library,
and it is the one worth re-establishing after any large change: not "the
files are there" but "the audio in them plays".

Version in `musaeus/__init__.py` is **0.1.0**. Treat that as accurate rather
than as modesty — see §5, and see the closing note of this section.

### What runs where

`VAULT_ROOT` (`/mnt/FORGE2TB/Projects/MUSAEUS_VAULT`) holds `INBOX` (arrivals,
mutable), `STAGING`, `QUARANTINE`, `RUNS` (logs and reports), `MetaData` (the
authorities of §3), and `Libraries/` containing the three editions of §4. Each
is independently overridable by environment variable — `MUSAEUS_INBOX`,
`MUSAEUS_ALAC_LIBRARY` and so on (`config.py:16-21`, `130-146`).

**Physical presence in `ALAC-Library` is meant to be a trustworthy signal that
does not depend on the database** (`config.py:23-31`). That is why the
cross-batch hash index lives *under `ALAC-Library` itself* rather than in the
vault DB — so a database wipe cannot destroy the record of what was already
finalised. The owner wiped the database from the console on 2026-09-07; that
decision is why it survived.

A Docker image exists, pinning Debian 12, Python 3.11, ffmpeg 5.1 and fpcalc
1.5.1, so that a bug report describes the same encoder the maintainer has. It
is for trying MUSAEUS on a machine that is not this one. It is not how the
library is run.

### The four principles it actually holds to

`musaeus/__init__.py` lists five architectural commitments. Four hold:

- **One `RunContext`** shared by every stage; one DB connection, one scan
  pass, one log session per run.
- **Content-addressed identity.** `audio_hash` is a SHA-256 of decoded PCM,
  so re-tagging and container rewriting do not change what a file *is*. This
  is why the deny list blocks *a recording* and not *a song* (§3).
- **Every stage must implement `dry_run()` — never optional.**
- **Propose, do not decide.** Anything requiring judgement emits a CSV a human
  rules on. The cover rule, the knock-off rule and the FM-radio identifier are
  all deliberately unautomated; see §4 and §7.

The fifth — *"Event log as the source of truth (DB is derived, always
rebuildable)"* — **is not true.** It is the most load-bearing false statement
in the codebase, and §2 is largely about that.

### Its relationship to ORPHEUS

ORPHEUS is the predecessor and is to be suspended or archived, not developed.
MUSAEUS was written clean-room rather than forked, which is why ORPHEUS
capabilities keep resurfacing as wishlist items rather than as code: the
FM-radio version identifier and the discography-gap report both exist as
ORPHEUS scripts and neither has been ported
(`docs/reconstruction/MUSAEUS_WISHLIST.md` §2, §3). Where an ORPHEUS technique
was measured and found wanting it is recorded as a rejection instead — the
absolute duration floor in §5, the chart-based knock-off rule in §7.

### What "0.1.0" means in practice

There is a live specification, `musaeus-consumer-readiness`, whose entire
purpose is to make this system safe for someone who is not its author. As of
2026-09-08 it has 26 tasks, 18 marked complete, and **13 of those 18 are
marked on hedged evidence only** — implementing module present, dedicated test
file green, acceptance criteria never individually audited. A fixture-only
rehearsal (P0-19) exists specifically to convert those claims into proof.

Read §5 before believing any green tick in this system, and read §2 before
believing that the database can be rebuilt.

---

## 2. The data model

**There are two schemas in this codebase. Only one of them is real.** Nothing
else in this section matters as much, and it is invisible from any single
file.

`db.py`'s `open_db()` creates the tables the pipeline actually uses
(`db.py:363-398`). A second, newer, versioned schema lives under
`musaeus/state/` — `state_metadata`, `schema_migrations`, `canonical_events`,
the projection tables, and a typed `duplicates` — and it comes into existence
only if `musaeus.state.migrator.migrate()` is called. **Nothing calls it.**
Outside `musaeus/state/` and its own tests there is no live caller of
`migrate()`, `DuplicateRepository`, `append_event` or the projector; the only
cross-imports are `preflight.py` reading the version read-only, and a shared
recovery-root assertion. `state/schema.py:16-27` says so in as many words:
this module deliberately does not change `db.py`'s behaviour, and wiring
happens later.

The consequence, stated plainly because it is easy to miss: **a live vault
database is an unversioned legacy database with none of the P0 state tables in
it.** The modules exist, their tests pass, and the running system does not use
them. That is the §5 pattern — a green check measuring nothing — reproduced at
architectural scale.

### The live tables

Created by `_SCHEMA` (`db.py:22-145`) in `musaeus.db`:

| table | key | what it is |
|---|---|---|
| `archive` | `file_path` UNIQUE | **the library** — one row per known file |
| `events` | `id` only | the append-by-convention legacy log |
| `duplicates` | none | staging for human review, never a resolution |
| `validation_issues` | `(file_path, issue)` | findings, regenerated per run |
| `metadata_cache` | `file_path` | raw ffprobe output, always rebuildable |
| `archive_tier_hashes` | `path` | the bit-rot baseline |

Three more SQLite files sit outside `musaeus.db` deliberately, so that wiping
the vault DB does not destroy them: `finalized_hashes` and `denied_hashes`
share one file under `ALAC-Library` (`db.py:498-562`), and the MusicBrainz
caches `mb_artist`/`mb_release` are their own (`db.py:647-662`).

### `archive` is primary, whatever the docstrings say

`musaeus/__init__.py` and `db.py:16-20` both assert that the event log is the
source of truth and the archive is derived and always rebuildable. **The tree
contradicts them.** `rebuild.py` is disabled because the legacy log is lossy
*by design*: hashes were written truncated to 16 characters plus an ellipsis,
and album, genre, year, track, duration, sample_rate, channels and codec were
never recorded at all (`state/events.py:11-24`, restated at
`state/migrations/__init__.py:99-111`).

So the real position is the opposite of the stated one. `archive` holds
metadata that exists nowhere else, and `projector.py:352-357` is the one place
that says it out loud: replacing the projection tables is safe in a way that
`DELETE FROM archive` never was.

**If you take one operational rule from this section: there is no rebuild.
Back up `archive`.**

### Identity is confused between two columns

`archive`'s only UNIQUE constraint is on `file_path` (`db.py:41-71`). But the
system's *stated* identity is `audio_hash`, which is merely indexed. Every
consequence of that mismatch shows up elsewhere in this document: rows going
ghost when files move, the bit-rot baseline keyed on path and orphaned by
every stage that renames (§5), `file_path` following the edition so a master
with a baked copy carries no row of its own (§4).

### Three traps in the live schema

**`INSERT OR IGNORE` into `duplicates` ignores nothing.** The legacy table has
no UNIQUE constraint at all (`db.py:77-88`), and four stages insert into it
with `OR IGNORE` — sentinel, cross_dupe, neardupe, acousticid. `OR IGNORE`
suppresses constraint violations; with no constraint there is nothing to
violate, so duplicate rows accumulate silently. The typed replacement does
have the UNIQUE key (`state/duplicates.py:66-84`) and is not wired in.

**`duplicates` is defined twice, incompatibly.** Legacy at `db.py:77-86`
(`group_id, file_path, duplicate_type, confidence, status, run_id, staged_at`)
and typed at `state/duplicates.py:66-84` (`run_id, candidate_item_id,
matched_item_id, detector, provider_recording_id, fingerprint_digest, score,
evidence_json, decision_status, created_at, evidence_identity`). The stages
write the first. Any document, test or query about "the duplicates table" must
say which one it means. The AcoustID insertion contract is declared as data —
`INSERTION_COLUMNS`, `state/duplicates.py:52-63` — precisely so the column
order stops being folklore.

**The schema depends on which stages have ever run.** Beyond the additive
`_MIGRATIONS` loop, ten stages add their own columns lazily via
`ensure_columns()` (`db.py:298-337`). A vault that has never run AcousticID
does not have the five AcoustID columns (`db.py:333-336`). Two installations
of MUSAEUS at the same version can therefore hold different schemas, and a
query that works on one fails on the other.

One piece of history worth keeping, because it explains an odd key:
`validation_issues` is keyed `(file_path, issue)` with **no `run_id`**. With
`run_id` in the key the table reached 343,938 rows, was pruned to 15,604 on
2026-08-24, and regrew 30–50k per run (`db.py:90-100`). Re-keying is done by
rebuilding the table, not by `ALTER` (`db.py:247-295`).

### What the unwired layer would give, if wired

Worth knowing, because the design decisions in it are good and the work is
already done:

- **A closed event vocabulary.** 25 types with declared required payload
  fields, validated on append; unknown types rejected; prose rejected where
  typed arrays are required; and any nested key matching
  `api_key/password/secret/token/credential` refused recursively
  (`state/events.py:56-162`, `256-321`). The legacy log by contrast has an
  open vocabulary — 43 types emitted, 40 of them known-lossy.
- **Append-only actually enforced**, by three rules rather than by comment:
  a repeated `event_id` is an idempotent no-op, a different event claiming an
  existing `(run_id, sequence)` raises, and `sequence` must exceed the run's
  high-water mark. No UPDATE or DELETE path exists on the table
  (`state/events.py:339-422`).
- **One projection function.** `apply_event()` is pure — no DB, no clock, no
  I/O — and `project()` is a fold of it (`state/projector.py:137-224`,
  `271-277`). The module exists because `rebuild.py`'s dispatch table listed
  ten event names the pipeline had stopped emitting, so every branch was dead
  and nothing said so.
- **A rebuild that refuses rather than guesses.** Unmappable legacy rows
  become `legacy.unmapped` with a payload digest, and a single one marks the
  whole run `BLOCKED` — run-scoped deliberately, because the stage attribution
  is itself part of what could not be reconstructed
  (`state/projector.py:186-206`).
- **Migrations as data, not functions**, checksummed over their SQL, with
  `validate_registry()` enforcing that the chain is contiguous and ends
  exactly at `SCHEMA_VERSION` — so bumping the constant without writing the
  migration fails at validation rather than at some later run
  (`state/migrations/__init__.py:48-77`, `194-242`).
- **Version in a table, not `PRAGMA user_version`**, because `user_version` is
  invisible to `.dump`/`iterdump` and the equality artefacts could not see
  version drift (`state/schema.py:87-105`).

Two details in that layer are load-bearing and non-obvious. `_connect()` sets
`isolation_level=None` because Python's `sqlite3` will not open a transaction
for DDL, so without it a `ROLLBACK` after a failed `CREATE` would undo nothing
(`state/migrator.py:158-165`). And backups use SQLite's online backup API
rather than `shutil.copy`, because of WAL — then reopen, `PRAGMA
integrity_check` must return exactly `ok`, and the bytes are re-hashed
(`state/migrator.py:200-273`).

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

**Resolved the same day, and the resolution has its own lesson.** A fresh
baseline recorded byte hash *and* PCM identity for **15,813 of 15,816**
archive files; the 3 failures were files deleted from under it mid-sweep, and
it reported them as errors rather than passing. **1,388 unmatchable rows were
then pruned** — every one pointing into `VAULT_ROOT/ALAC_Archive/...`, the
layout used before the `Libraries/` reorganisation, which is how thoroughly a
path-keyed baseline rots.

The prune rule is narrower than "the path is gone", deliberately: a file that
*moved* also has a vanished path, and its row is precisely what makes the move
recognisable. It deletes only rows that are **both** pathless **and** without
a PCM identity. The first version of that script asked for confirmation on
stdin while its own heredoc held stdin — so it hit `EOFError` and deleted
nothing. That is the shape a destructive tool should fail in.

### One value answering two questions

**An unreadable source would have deleted the last playable copy of its own
recording.** M-01 in the Repair Register — the entry marked *START HERE — THIS
ONE DESTROYS DATA* — fixed 2026-09-08 in commit `88ecf5c`.

The CAR builder's resume check read:

```python
if _output_matches_source(file_path, output_file):
    return "SKIP DONE ..."
output_file.unlink()
```

`_output_matches_source()` (`scripts/car_library/vendor/build_aac_library.py:530`)
compares source and output durations and returns `False` for two situations
that are not alike at all:

| what actually happened | deleting the output is |
|---|---|
| the output is wrong | correct — re-encode it |
| the source could not be read | catastrophic — that encode is the last file that still plays |

Only the first justifies destroying anything. The second is precisely when the
car copy matters most: the master is gone, moved or rotted, and the encode made
from it while it was healthy is all that survives.

This was **reachable from an ordinary `--from-catalogue` build**, not an edge
case — any row whose master has been deleted since its encode was made.

**Guard:** the caller now probes the source before unlinking and raises rather
than deletes when it cannot be read (`build_aac_library.py:604-625`).
`_output_matches_source` is deliberately left exactly as it was — it answers a
narrow question correctly, and the defect was never in the answer. Test:
`tests/test_aac_unreadable_source_keeps_output.py`.

**It had never fired.** No `CATALOGUED` row was missing its file on the day it
was found, so the trigger did not exist.

**Lesson:** a latent defect whose cost is silent permanent loss still earns a
test, because the condition that arms it is one deletion away and this system
deletes on rulings routinely. More generally — **a boolean that can mean two
things will eventually be read as the wrong one.** Where `False` conflates
"wrong" with "unknown", the damage happens at the caller, not at the check.

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

### Standing hazards

Carried across from `MUSAEUS_TODO.md` because they are not defects with
guards — they are conditions that stay true and will bite again.

**A long-running process runs the code it imported at startup.** Six fixes
committed 2026-09-03 were inert for the run already in flight. Fixing a bug
does not fix the process currently executing the bug; either restart it or
accept that this run has the old behaviour, and say which.

**Measure the artefact, not the report — and "decodes clean" is not measuring
it.** A re-encode *of* truncated audio decodes perfectly. Compare duration;
where it matters, compare the PCM hash. This is the hazard that the CAR
builder's source-versus-output duration check defends against, and that the
ALAC bake lacked until a pre-bake gate was added.

**Silence is not evidence, and neither is noise.** A seal that cries wolf is
discarded as fast as one that lies. Verify what the row *claimed*, not merely
that the check ran.

**Check the model before trusting a session with vault mutations.**
`echo "${ANTHROPIC_BASE_URL:-unset}"` — unset means a direct connection. A
proxied or downgraded model holding authority over a 936 MB database and
613 GB of masters is a risk taken by accident, in an environment variable,
with nothing announcing it.

---

## 6. The pipeline

**Order is the architecture.** Thirty stages, and the sequence carries as much
design as the stages do. `stages/__init__.py` is unusual in that it explains
*why* each stage sits where it does, often citing the run that proved it — that
file is the most valuable thing in the repository and should be read before it
is edited.

`CANONICAL_PIPELINE` is Act 1 + Act 2 + Act 3 + Enrichment, and
`DEFAULT_PIPELINE` is an alias for it, not a separate list
(`stages/__init__.py:422-430`).

| act | stages | in order |
|---|---|---|
| **1 — intake & correction** | 16 | Preflight, Ingest, Permissions, Sentinel, DenyList, Scholar, Health, Corrupt, AlbumArt, Normalize, SpellCheck, Sanitize, ArtistConsolidate, VariousArtistsFix, TributeQuarantine, GenreValidate |
| **2 — dedup & staging** | 4 | CrossDupe, NearDupe, DupeResolver, ClassicalComposer |
| **3 — canonicalise & finalise** | 7 | Canonicalize, Finalize, BPM, Forge, Tagger, Organize, Audit |
| **enrichment** | 3 | Enrich, MBEnrich, IdentityTag |

Four narrower pipelines exist for specific jobs: `FULL_PIPELINE`
(`run --full`, 8 stages), `ARCHIVE_PIPELINE` (`--archive`, 15),
`MAINTAIN_PIPELINE` (`--maintain`, 9) and `ENRICH_PIPELINE` (`--enrich`, 3),
at `stages/__init__.py:433-482`. `cli.py` only selects among these constants;
there is no separate runner module — iteration lives in `cli.py`'s
`_run_pipeline` and, separately, in `console.py`.

### Why the order is what it is

Each of these was paid for. They are the reason not to "tidy" this list.

**Everything hygienic waits for Scholar.** Corrupt, Health, Normalize,
Sanitize and ArtistConsolidate all select on `status='CATALOGUED'` and read
codec, bitrate and duration. A real data dependency, not a style choice
(`stages/__init__.py:249-257`).

**DenyList sits immediately after Sentinel** — the first moment an
`audio_hash` exists, and therefore the earliest point a file about to be
refused can be refused, before any stage invests work in it
(`stages/__init__.py:52-57`).

**TributeQuarantine after the name-settling stages, before GenreValidate.** It
matches on artist, title and album, so it must read settled names; and it is
the only stage that *removes* work, so it belongs ahead of per-row cost
(`stages/__init__.py:297-321`).

**GenreValidate after ArtistConsolidate and VariousArtistsFix**, because the
law is keyed on artist. Before 2026-08-24 it ran on demand only — which is why
a file's genre came from its own tags via Scholar and nothing ever corrected it
against MasterLaw (`stages/__init__.py:322-328`).

**AlbumArt before Canonicalize**, so embedded art survives the container
conversion rather than being embedded into a file that is then rewritten
(`stages/__init__.py:96-101`).

**ClassicalComposer at the head of Act 2 — after dedup was the wrong answer,
and so was before.** Filing under composer collapses dozens of performers into
one artist (Bach 60 tracks, Vivaldi 52), and NearDupe compares titles *within*
an artist. On 2026-08-25 that quarantined 15 distinct recordings as near-dupes,
including two different movements of the Water Music Suite at 89%. The
attribution created the surface dedup then tripped on
(`stages/__init__.py:335-350`). It still needs GenreValidate first — "is this
Classical?" precedes "who wrote it?" (`classical_composer.py:11-12`).

**DupeResolver last in Act 2**, so a confirmed duplicate is physically pulled
before Act 3 spends ffmpeg on it (`stages/__init__.py:258-265`). This is also
why Canonicalize was moved back into Act 3 after the 2026-08-17 experiment.

**Finalize before Forge and Tagger**, deliberately, so an archival copy can be
taken of the canonicalised-but-not-yet-loudness-tagged file
(`stages/__init__.py:266-272`).

**Organize after Tagger, before Audit.** After Tagger because organising
earlier names folders from tags Tagger is about to change; before Audit because
with Organize *after* it, every audit result described a layout Organize then
rewrote, and the gate passed on pre-move state
(`stages/__init__.py:361-377`).

**Enrichment last, after Audit**, isolated from the file-safety-critical
stages so a network hiccup cannot interfere. For the same reason
VariousArtistsFix runs inside `DEFAULT_PIPELINE` with MusicBrainz lookups
forced off (`various_artists_no_mb=True`, `cli.py:1565-1571`).

**IdentityTag dead last.** It writes identity into the *files*, so it must run
after everything that resolves identity — otherwise it writes what the run is
about to learn. Its absence is why roughly 8,074 MBIDs lived only in a database
that was later reset to 87 rows (`stages/__init__.py:411-415`).

### Ordering as a place defects hide

§5 records that `corrupt` running before `albumart` is why a decode defect
involving embedded cover art could never fire. That is not a one-off; it is a
category. Two more of the same shape:

**A stage whose scope was so narrow it never executed.** Permissions swept
`ctx.inbox` only, and INBOX is empty or already correct most of the time, so
the chmod path reported success without ever doing anything — for a month.
Scope widened 2026-08-21 (`permissions.py:7-12`).

**A stage whose absence was the only thing protecting the library.** Organize
built every target path under `ctx.inbox` while selecting `CATALOGUED` rows,
which after Finalize live in ALAC-Library. Running it would have moved 10,660
of 10,660 files out of the library back into INBOX to be re-ingested. Fixed
2026-08-29, wired 2026-08-30 (`organize.py:7-31`).

### What each stage is allowed to touch

Three stages in the default pipeline never write to the database at all:
**Preflight, SpellCheck, Audit**. Preflight accumulates OK/WARN/FAIL and never
aborts — the caller decides (`preflight.py:3-12`). Audit is a report-only
physical-presence gate. SpellCheck is report-only by design.

Four write findings or events but never `archive`: **Health**
(`validation_issues`), **Tagger** (events plus file tags), **CrossDupe** and
**NearDupe** (`duplicates` plus events).

The remaining twenty-three write `archive`. A different and more dangerous
axis is which ones **mutate files on disk**: Permissions, Corrupt, AlbumArt,
TributeQuarantine, DupeResolver, ClassicalComposer, Canonicalize, Finalize,
Organize, BPM, Forge, Tagger, IdentityTag. Note that AlbumArt rewrites the
file to embed art, and that DupeResolver, ClassicalComposer, Canonicalize,
Finalize and Organize all move or rename — which is exactly what orphans
path-keyed baseline rows (§5).

**Every stage must implement `dry_run()`; it is never optional**
(`musaeus/__init__.py`). Treat a stage without a meaningful preview as a
defect, not a gap.

**But do not close that gap by calling the stage's own `dry_run()` from the
CLI.** Every stage has one and it looks like the obvious fix; using it would
undo P0-02. `--dry-run` routes to the planner deliberately, so that it no
longer means "execute with a flag set" — the planner never instantiates a
stage, never opens a writable connection, and never calls `ensure_dirs()`.
The correct fix is a pure `plan_candidates(conn, cfg)` on the stage, which
the planner already calls where one exists. Ten stages have one; twenty-one
do not. (M-13 in the Repair Register: this document and MUSAEUS_TODO.md
disagreed, and the TODO was right.)

### Deliberately out of the default pipeline

Six stages are on-demand only: **Auditor, Curator, Playlist, Transcode,
BitRot, OriginalYear**. Ghost and Integrity are absent from the default but
present in the archive and maintain pipelines.

Two exclusions are cost decisions with numbers behind them, and both should be
left alone until the numbers change:

**OriginalYear** makes one rate-limited network call per track, about three
hours for the library (`stages/__init__.py:60-66`).

**AcousticID** was wired in on 2026-08-30 and produced a 21-hour full-library
fingerprint pass that reached 7,545 of 10,656 **while holding the write
lock**. It was deferred 2026-08-31. `acousticid_checked_at` makes it resumable
and the intent is to re-wire once the remaining ~3,100 are done. Its one pass
found 319 acoustic duplicates with *different* audio hashes — findings PCM
hashing structurally cannot make, which is the argument for finishing it
(`stages/__init__.py:384-410`).

Three docstrings in this area are stale and should not be trusted:
`stages/__init__.py:135-137` still lists Organize as on-demand and does not
mention AcousticID's deferral; `organize.py:31` still says it is absent from
the default pipeline; `state/duplicates.py:9-24` describes defects that have
since been fixed.

---

## 7. What MUSAEUS deliberately does not do

**A rejection with a measurement behind it is worth more than a feature.**
This section exists so that nobody rebuilds something that was already built,
measured and thrown away. Each entry names what would go wrong.

Three lists must not be confused. **TODO** protects what exists — leaving an
item undone means something is unprotected or a check is lying. **WISHLIST**
makes the library bigger or better-chosen, and costs nothing if never done.
**This section** is neither: these are settled refusals. If forced to choose
between the first two, take the TODO — a bigger library that cannot be trusted
is worse than a smaller one that can
(`docs/reconstruction/MUSAEUS_WISHLIST.md`).

### Measured, then rejected

**Lossless-fraud detection by spectral cutoff.** Built and measured
2026-09-07 against 20 known-lossy and 20 known-lossless sources: **80%
overlap**, and every threshold mislabels at least as many genuine lossless
files as it catches transcodes. Modern AAC at 256 kbps reaches 20–21 kHz,
while genuine lossless transfers of 1950s–70s masters are band-limited by the
*tape* — The Who's *The Kids Are Alright* tops out at 14.7 kHz, one FLAC at
9.9 kHz. Shipping it would flag the oldest and most valuable recordings in the
library as frauds. **Do not rebuild this.**

**An absolute duration floor.** ORPHEUS used `duration < 1.0s`, which caught a
0-second file and missed two at exactly one second. Raising the floor is
worse, not better: a 120-second floor flags **397 complete recordings** —
*Hit the Road Jack* at 2:00, *All Shook Up* at 1:58. Early rock and 60s pop
are supposed to be short. The working replacement compares a file against
another copy of the same recording, which took detection from 6 of 11 to 11
of 11 (§5).

**A chart-based knock-off rule.** Rejected 2026-09-07. "Did it chart" needs
external data MUSAEUS cannot verify offline, and would delete session players,
B-sides, album tracks and most of the jazz and blues. The cheaper signal that
does work is *an artist no authority has ever ruled on* —
`scripts/artists_to_review.py`.

**Further mechanical recovery of the no-genre rows.** Two rounds were run and
**each returned exactly one row.** The remainder need either an external
source or a ruling; grinding the same mechanism a third time is not worth the
run. The external-source route was subsequently built as
`scripts/itunes_genre_fill.py`, which is the right answer to this and is
recorded in the wishlist as done.

### Not being restarted

Both of these are live refusals, not oversights, and neither should be
switched back on casually.

**The overnight cron stays disabled.** Re-enabling it is deliberately not
done. An unattended schedule on a system whose preview path is still being
fixture-proven is the one combination the consumer-readiness work exists to
prevent — see §1 and the P0 spec's own scope note, which withholds any
scheduler change.

**ORPHEUS is not being revived as a running pipeline.** It is the predecessor,
to be suspended or archived. Its techniques are mined deliberately — two of
its scripts sit on the wishlist unported, and two of its heuristics are
rejected with measurements in this section — but it does not run again.

### Judgement that stays with the owner

**The cover rule is not automated, on purpose.** A cover is in if it is the
artist's own work, or if it charted for whoever released it. Springsteen's own
*Blinded by the Light* never charted; Manfred Mann's went to number one. Both
stay. **A mechanical rule destroys the first** (§4).

**A bare `cover` title pattern.** `\btribute\b` *is* a title pattern as of
2026-09-06 — that extension was reversed into existence on the owner's
instruction (commit `2c735e5`) after three knock-offs were found catalogued
whose artist names were innocent (*Classic Blues Tones*, *Scott D. Davis*,
*Led Zepagain*) and only the title gave them away. A `cover` pattern is
deliberately **not** added alongside it: it would have quarantined
Springsteen, and Morse/Portnoy/George. Same reasoning as the cover rule above,
one layer down.

**The FM-radio version identifier must propose, never decide.** It is wanted,
it is on the wishlist, and the constraint is recorded there in advance: it
needs MusicBrainz so cannot run offline, and it will be wrong on jazz,
classical and album cuts that were never singles. It emits a CSV
(`MUSAEUS_WISHLIST.md` §2).

**Rare genres go to review, never straight to the law.** Learned on the first
live run of the iTunes genre filler, which returned "Holiday" for a Blues
Brothers covers act. Ask the library before the API: where an artist's own
tracks already agree, that settles it (`MUSAEUS_WISHLIST.md` §1).

**Acquisition search via Prowlarr / Lidarr** — dropped on the owner's
instruction, 2026-09-07.

### Things that look like bugs and are not

**The fourth copy of the decode check is left alone deliberately.**
`scripts/car_library/vendor/orpheus_noise_generator.py` carries its own copy,
and it only ever checks the six noise beds the generator creates itself, which
carry no artwork — so the artwork defect cannot fire there. That is a reason,
not an oversight. Do not "fix" it without one (§5).

**The two protected-artist lists must not be merged.**
`canon/protected_artists.py` holds 13 names that must never be split on commas
or ampersands; `tribute_quarantine.py` holds a separate list of real artists
who must never be quarantined. They answer different questions (§3).

**Broad checks must not be narrowed to reduce noise.** `doctor`'s "removed
audio still held" had two benign hits out of three; narrowing it by reason
would have suppressed the one real defect in the same batch. The fix was to
make the check *explain* itself, not to quieten it (§5).

**The decode-stderr classifier is a denylist, and must stay one.** Drop lines
known to come from an image decoder, keep everything else. An unrecognised
image codec then leaks through as a false positive, lands in a review CSV, and
a human rules on it. An allowlist of known audio errors would fail the other
way — an unrecognised audio error would be dropped and a damaged master baked
into an edition. **Fail towards the human, not towards the encoder** (§5).

**Editions are never built from other editions**, and masters are never baked.
An edition is derived and disposable; a master is not (§4).

**Two corrections to the Repair Register**, both verified 2026-09-08 and
recorded here because the Register will outlive the session that wrote it, and
someone will act on its framing.

*`build_aac_library.py` is no longer vendored byte-identically, and must not be
re-synced.* The Register describes MUSAEUS's copy as byte-identical to
ORPHEUS's. Measured: ORPHEUS's `SCRIPTS/build_aac_library.py` is **461 lines**;
MUSAEUS's `scripts/car_library/vendor/build_aac_library.py` is **816**, having
carried its own patches since 2026-08-16. Anyone following the Register's "fix
both copies" instruction by syncing them would silently revert every one of
those patches — including the M-01 guard in §5.

*M-01 does not exist in ORPHEUS.* It is a one-repository fix. The Register's
default assumption — that a defect in vendored code needs fixing in both places
— does not hold here, and looking for it in ORPHEUS is wasted effort.

**Three masters left `ALAC_Archive` on 2026-09-07/08, and it was not a
defect.** Recorded because it was investigated once and would otherwise be
investigated again. `bitrot --rebaseline` failed with three unreadable files —
*Spirit of the West — Homelands*, *Who, The — Cut My Hair*, *ZZ Top — Legs* —
and each had a `.bake_tmp.FAILED_VERIFY` artefact sitting in `ALAC-Library`,
which made it look as though a failed bake had consumed its own source.

It had not:

- **Three were deleted deliberately** on the owner's ruling, 2026-09-08, each
  carrying a `FILE_DELETED` event naming the keeper that replaced it.
- **A fourth `FAILED_VERIFY`, *Steve Miller Band — Blue Odyssey (2)*, was
  `DUPE_PURGED`** by dedupe-purge on **2026-09-05** — a different event three
  days earlier. That is why the group looked asymmetric: it was never part of
  the same story.
- **"The bake consumed its source" is ruled out by construction.** Each baker
  contains exactly two `.rename()` calls, and all four act on the temporary
  output — `build_alac_library.py:350` (temp → `.FAILED_VERIFY`) and `:590`
  (temp → target), with the same pair at `build_aac_library.py:378, 643`.
  Neither baker renames, moves or unlinks a source anywhere.

**Lesson:** a `FAILED_VERIFY` artefact beside a missing source is not evidence
that one caused the other. Check the event log for the deletion — the reason is
recorded there, with the keeper — before suspecting the code.

### Not currently possible, whatever the docstrings say

**Rebuilding the database from the event log.** `rebuild.py` is disabled and
should stay disabled: the legacy log truncated hashes to 16 characters and
never recorded album, genre, year, track, duration, sample_rate, channels or
codec. Two docstrings still claim the archive is derived and always
rebuildable. They are wrong (§2).

**`--big-kahuna` no longer exists.** Neither the flag nor
`BIG_KAHUNA_PIPELINE` appears anywhere in the codebase; what remains is
`musaeus curator --export-root`. A spec task written to guard the old flag was
closed as superseded on 2026-09-07 rather than implemented, because
implementing it would have added a fail-closed check for a command that cannot
be invoked.

---

*Sections 1–7 complete as of 2026-09-08. Figures marked as measured come from
`docs/reconstruction/MEASURED_2026-09-07.txt` or from the 2026-09-08 pass
recorded in §1 and §5; everything else carries a `file:line` and was read
from source.*

*Four of §2's load-bearing claims were re-verified against the live system on
2026-09-08 and all four held: nothing outside `musaeus/state/` and its tests
calls `migrate()`; the legacy `duplicates` table has no UNIQUE constraint;
`rebuild.py` is disabled; and the live database contains **none** of the P0
state tables. §2 is the section to trust and the section to act on.*

*A caution on this document's own authority: an earlier draft closed by
suggesting the event log could reconstruct what these sections assert. It
cannot — §2 explains why, and that correction is itself the best example of
the failure mode §5 catalogues. **Where this document and the code disagree,
read the code.** Where the code's docstrings and the code's behaviour
disagree, measure.*
