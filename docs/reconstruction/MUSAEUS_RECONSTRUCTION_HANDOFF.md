# Instruction: finish MUSAEUS_RECONSTRUCTION.md

**For whoever picks this up — VSCodium, Kiro, or a later Claude Code session.**
Written 2026-09-07 by the session that wrote sections 3, 4 and 5.

Paste this whole file as your prompt, or point the session at it.

---

## What exists

`~/Desktop/MUSAEUS_RECONSTRUCTION.md` holds **sections 3, 4 and 5**:

- **§3 The authorities** — the six stores of truth, and how they disagree
- **§4 Conventions and the rulings behind them** — the owner's decisions,
  each with the evidence that settled it
- **§5 The failure catalogue** — every real defect and the guard that now
  prevents it

Those three were written first **on purpose**. They hold judgement — rulings,
measurements, and the reasoning behind conventions that look arbitrary and are
not. That material existed only in one week's working context and would have
been lost.

## What is missing, and why it is a good task for you

Sections **1, 2, 6 and 7**. Every one is **transcription from a primary
source that still exists**. You are not being asked to reconstruct judgement.
You are being asked to read four specific things and write them down clearly.

That is why this task was left rather than rushed: it is the part that does
not need the context, and doing it badly is recoverable.

---

## If you are running in Kiro Web (or anywhere without the vault)

Kiro Web works on the **repository**. It can read every code source this
brief names — `musaeus/db.py`, `musaeus/stages/__init__.py` — because they
are in the repo. It **cannot** reach two things:

- `/mnt/FORGE2TB/Projects/MUSAEUS_VAULT/musaeus.db`, the live database
- the vault's `MetaData/` files

So the "measure it yourself" instruction below cannot be followed there.
**`docs/reconstruction/MEASURED_2026-09-07.txt` holds those figures**,
captured from the live vault on 2026-09-07.

Use them, and **say in the document that they are as of 2026-09-07 and were
not re-measured.** A dated figure a reader can check is honest; an undated
one implies a freshness it does not have.

If you ARE running with the vault mounted (Kiro's desktop app, the CLI, or
Claude Code locally), ignore this section and measure. Measured beats
quoted, always.

## The four sections and their exact sources

### §1 — What MUSAEUS is

**Source:** `MUSAEUS_RECONSTRUCTION.md` §3 and §4 (already written), plus
`/mnt/FORGE2TB/Projects/MUSAEUS/README.md` if present.

One page. A personal music-library pipeline that takes audio in, canonicalises
its metadata against a set of authorities, deduplicates it, and produces three
editions. Name the three tiers and the one-way rule (masters are never baked;
each edition bakes once from the masters; no edition is built from another) —
§4 already states it, so keep the wording consistent rather than paraphrasing.

Write this **last**, once you have read the rest. A summary written first is
a guess.

### §2 — The data model

**Source:** `musaeus/db.py`. The `CREATE TABLE` statements are at lines 24
(`events`), 41 (`archive`), 77 (`duplicates`), 101 (`validation_issues`),
112 (`metadata_cache`), 139 (`archive_tier_hashes`), and in the separate
hash-index schema 503 (`finalized_hashes`) and 534 (`denied_hashes`).

Cover:

- **`archive`** — the library itself. Every column, what it holds, and
  which are set by which stage. Several matter more than they look:
  `audio_hash` (PCM identity, survives re-tagging — this is what dedup and
  the deny list key on), `full_hash`, `filename` (the ORIGINAL name, which
  has rescued rows whose artist and title were never populated),
  `car_export_path`, `lufs_baked_at`, `decode_ok`, `finalized_at`.
- **The statuses**, and what each *means* rather than just its name.
  Measure the live counts; do not copy these:
  `CATALOGUED`, `DELETED`, `GHOST` (the file is gone from disk — a one-way
  door since 2026-08-31), `DUPE_REVIEW`, `QUARANTINED`, `PENDING`,
  `TRIBUTE_REVIEW`.
- **`events`** — the audit trail, and the most important table in the system
  for reconstruction. Every deletion records its reason and `audio_hash`.
  Note that `musaeus/rebuild.py` can rebuild `archive` state from it.
  Report the live count and the number of distinct `event_type` values.

### §6 — The pipeline

**Source:** `musaeus/stages/__init__.py`, the `DEFAULT_PIPELINE` tuple, and
the module docstring above it which already groups the stages into acts.

Derive the order — do not copy this list, it will drift:

```bash
python3 -c "from musaeus.stages import DEFAULT_PIPELINE as P; \
  print(' -> '.join(getattr(c,'NAME',c.__name__) for c in P))"
```

At time of writing that is 30 stages, `preflight` through `identity-tag`.

For each stage, one or two lines: **what it guarantees**, not how it works.
Read the module docstring of each — they are unusually good and several
record the incident that caused the stage to exist.

Say explicitly which stages are **not** in `DEFAULT_PIPELINE` and are run on
demand (`dupe-resolver` has a standalone CLI entry, for instance), because a
reader will otherwise assume the list is everything.

### §7 — Deliberately not doing

**Source:** the `## Deliberately not doing` section of
`~/Desktop/MUSAEUS_done/MUSAEUS_TODO.md`, plus the rejected
lossless-fraud entry in that file's P1 block.

This is nearly written already. Your job is to move it across **with its
measurements intact**. Each entry must carry the number that settled it:

- a cover-detection rule would destroy Springsteen's own *Blinded by the
  Light*, which never charted, while Manfred Mann's cover went to #1
- a 120-second duration floor flags **397 complete recordings**, 2.4% of the
  library
- lossless-fraud detection by spectral cutoff shows **80% overlap** between
  known-lossy and known-lossless sources

An entry without its number will be overturned by the next reader who thinks
it sounds like a good idea.

---

## House style — what makes §3–§5 work

1. **Every number is measured, never carried forward.** If you write a count,
   run the query first. Several documents in this project drifted precisely
   because figures were copied between them.
2. **State the evidence beside the claim.** "Do not raise the floor" is an
   opinion. "Do not raise the floor: 120s flags 397 complete recordings
   including *Hit the Road Jack* at 2:00" is a fact a reader can act on.
3. **Name the incident.** Where a rule exists because something went wrong,
   say what went wrong. That is what stops it being undone.
4. **Prefer a table to a paragraph** for anything enumerable.
5. **Write for someone with no context**, in October, alone.

## The one hard constraint

**Do not invent rationale.**

The single most damaging thing you could do to this document is supply a
plausible-sounding reason for a convention whose real reason you could not
find. A future reader cannot tell your guess from the owner's ruling, and
will act on it.

If you cannot source a *why*, write the *what* and say the reason is
unrecorded. That is a useful sentence. A confident invention is not.

The same applies to numbers: if a query fails or a file is missing, say so.
Do not estimate.

## Verify before you finish

```bash
cd /mnt/FORGE2TB/Projects/MUSAEUS
python3 -m pytest -q          # should be green
python3 -m musaeus doctor     # the current health report
```

If `doctor` disagrees with anything you have written, `doctor` is right and
the document is wrong. Fix the document.

---

## Ordering

Do **§2, then §6, then §7, then §1**. Section 1 is a summary and should be
written once you have read everything else.
