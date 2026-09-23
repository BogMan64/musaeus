# MUSAEUS — Project Scope

> **Brought into the repository 2026-09-17.** Until today the only copies
> lived at `~/MUSAEUS_RECOVERY_KIT/SOURCE_DOCUMENTS/` and
> `~/Desktop/MUSAEUS_done/`, neither of them version-controlled, and it had
> gone 13 days without an update while four of its standing rules changed
> underneath it. Same failure as `MUSAEUS_EDITIONS_VOCABULARY.md` on
> 2026-09-14 and `artist_form.py` the same morning: the rule existed, was
> correct, and could not be seen from where it was needed.
>
> **The working copy is `docs/reconstruction/MUSAEUS_PROJECT_SCOPE.md`. Edit
> that one and commit.** `~/Desktop/MUSAEUS_PROJECT_SCOPE.md` is a published
> artefact refreshed from it and is not version-controlled — do not edit it,
> for the reason recorded in `MUSAEUS_RECONSTRUCTION.md`: on the Desktop a
> collision is an edit that silently never happened.

A living document, not a status report — the handoff doc covers "where
things stand right now"; this covers "what this project actually is, and
what its standing rules are." Grey to review, correct, and extend —
anything missing here is fair game to add.

*Last folded in: 2026-09-19 (Claude Code) — see §9. Before that,
2026-09-04 (Claude Code) — SOPs §4.27-4.30
(verify the claim not a constant; 255 BYTES per path component;
a wanted-list entry must clear already_owned), the ForClaudeHandoff
rename, and the curation rule that the test is whether the RELEASE is
the artist's own work. Includes §4.30, the PASSTHROUGH blind spot
(a rejected truncated file reached the library; resolved, see §9). Before that, 2026-08-19 (second Claude
Code session) —
`orpheus_noise_generator.py` vendored and built (§6), the last of four
gaps a full 222-script ORPHEUS audit surfaced, split by ownership
between the two sessions; confirmed working via real execution
(-16.1 LUFS measured against a -16.0 target), new
`scripts/car_library/generate_noise.py` wrapper, 4 tests. Before that,
2026-08-18 (first Claude Code session) — the
`DupeResolverStage` staleness bug fixed AND verified live (422 pending
rows → 1), the `FinalizeStage`/`ALAC_Archive` gap confirmed and handled
operationally rather than in code (`build_alac_library.py` now warns
instead), Phase 1 run end-to-end against the real vault, and Phase 3
committed (§5) after independent review. Before that, same day (second
Claude Code session): Phase 3 (USB wipe/transfer) built as a standalone
script (§5), safety-gate design confirmed with Grey before any code was
written, one real fail-closed-vs-fail-open bug caught by its own test
suite before shipping, and one concrete finding from a real (read-only)
device check: `/dev/sdb` on this machine is currently a real 8TB backup
drive, not a spare test device. Before that, same day (first Claude Code
session): Phase 2A/2B
built and committed (§5), a real bug found and fixed in AuditStage's
hash-index check (10,415 false positives, root-caused and verified fixed
against the real vault), and a newly-found (not yet fixed) staleness
issue in the `duplicates` table behind part of the known 12,242
dupe-resolver errors. Before that, same day: Phase 1/2A/2B/3 roadmap and
the ALAC_Archive migration script (§5), corrected per a second Claude
Code session's cross-check to record Phase 2A/2B as standalone scripts,
not new pipeline Stages. Before that, same day:
pipeline reorder (Canonicalize ahead of dedup, Permissions/Enrich/MBEnrich
into DEFAULT_PIPELINE), the resulting §4.2 tradeoff and its documentation,
and same-day doc/git cleanup. Before that: 2026-08-17, four passes that same
day (see §9 for the full pass-by-pass history — first from a pasted
Claude Code transcript cross-checked against prior handoffs, second from
a parallel data-loss-recovery session, third a documentation-completeness
recheck, fourth the §7 structural cut). See §9 for what changed in each
pass — passes were cross-checked against each other where they overlap,
and real discrepancies were found and flagged rather than silently
resolved (§2; flagged findings now tracked in `MUSAEUS_OPEN_ITEMS.md`
rather than a scope-doc §7). Convention going forward: this line is
updated to the most recent pass's date every time new work is folded in,
not just the inline per-entry date-stamps already used throughout.*

---

## 1. Purpose

MUSAEUS is a personal Python/SQLite pipeline that ingests, cleans,
deduplicates, canonicalizes, and organizes Grey's personal ALAC music
library (currently in the 20,000–35,000-track range and growing). It is
the deliberate successor to **ORPHEUS**, a predecessor project suspended
after being damaged by free-tier AI tooling (Kiro IDE, a weak-model
auto-commit loop) that introduced silent, confident-looking corruption —
fictional architecture docs, code written against contracts that were
never real, and actual data corruption that went unnoticed for a time.
MUSAEUS's entire operating discipline exists as a direct response to that
history.

That discipline has since been tested for real, not just in principle: an
automation script descended from ORPHEUS and left running against
MUSAEUS's own repository (`orpheus_overnight_self_heal.py`, on a cron
job) reproduced the same failure mode — unreviewed auto-commits and a
corrupted work-in-progress state — before it was caught and disabled (see
§4.10, §8). The risk this project's rules exist to guard against isn't
hypothetical; it already happened once inside MUSAEUS itself.

## 2. System components

- **MUSAEUS core pipeline** — `/mnt/FORGE2TB/Projects/MUSAEUS`, backed by
  SQLite (`musaeus.db`). Vault at
  `/mnt/FORGE2TB/Projects/MUSAEUS_VAULT`, containing `INBOX`,
  `ALAC-Library`, `MetaData`, `QUARANTINE`, `RUNS`, `STAGING`. `STAGING`
  is a transient write-here-first-then-verify buffer used during the
  Canonicalize → Finalize handoff (see §5 for the full flow) — it should
  be empty at rest, not a permanent tier.
- **`MUSAEUS_TEST_VAULT`** — a separate, persistent test vault, distinct
  from the real `MUSAEUS_VAULT`. Confirmed untouched during the
  2026-08-11 real-vault hard reset (its DB file was three days stale at
  the time), which is what made it possible to confirm the wipe was
  scoped to the real vault only (see §4.13). Distinct from the
  consumer-readiness safety spec's disposable per-test fixture harness
  (`P0-01`, `.kiro/specs/musaeus-consumer-readiness/tasks.md`), which is
  ephemeral rather than a standing directory.
- **Pipeline stage order** — read directly out of the live
  `DEFAULT_PIPELINE` object (not the source text) on 2026-08-21:
  `Preflight → Ingest → Permissions → Sentinel → Scholar → Health →
  Corrupt → AlbumArt → Normalize → Sanitize → ArtistConsolidate →
  VariousArtistsFix → CrossDupe → NearDupe → DupeResolver → Canonicalize
  → Finalize → BPM → Forge → Tagger → Audit → Enrich → MBEnrich`
  (23 stages). `BPMStage` and `VariousArtistsFixStage` joined the default
  chain 2026-08-19/20 — see §6 for their full descriptions.

  **Two positions worth noting, because a 2026-08-21 draft of this bullet
  guessed both wrong and the guesses were plausible:**
  `VariousArtistsFix` sits at **12**, inside Act 1 immediately after
  `ArtistConsolidate` — it is an artist-name repair, so it must land
  before dedupe reads those names, not after `BPM`. And `BPM` sits at
  **18, between `Finalize` and `Forge`** — not after `Tagger`. §6's
  "late, near Forge, after Finalize" phrasing is satisfied by this slot;
  interpolating from prose put it two places too late. Verify this bullet
  by importing the list, never by reading the file — see §4.16.

  Still genuinely on-demand only, not part of the default chain:
  `AcousticID`, `Reviewer`, `Organize`, `Integrity`. This settles the
  "canon-before-dedupe" question raised in earlier passes **for the
  artist-name sense of "canon"**: `ArtistConsolidateStage` (Act 1) does
  run before `CrossDupeStage`/`DupeResolverStage` (Act 2), by design.
  See §4.15 for the terminology trap this raised.
- **ORPHEUS** — the predecessor, currently suspended for automated
  pipeline use but under active manual repair by Grey in a separate,
  parallel session (not part of MUSAEUS's own scope, but MUSAEUS has
  salvaged specific proven scripts from it — see §6).
- **Car-Library** (`scripts/car_library/build_car_library.py`) —
  standalone tool, deliberately independent of the main pipeline. Not
  imported by `musaeus/stages/__init__.py` or `cli.py`, never touched by
  `musaeus run` or cron. Manually invoked, reads from
  `RUNS/AAC-Car-Masked`. Grey refined the independence requirements
  further in a 2026-08-15 session (dedicated Grey-populated input folder
  rather than a full-library scan, single command with a runtime Y/n
  masking prompt). **Confirmed compliant, 2026-08-17 (Claude Code, direct
  code read):** `find_input_files()` only scans `RUNS/AAC-Car-Masked/`,
  never the full library; one script run does encode + optional masking
  in sequence behind a single `[y/N]` prompt (`--mask`/`--no-mask` skip
  it entirely). Both refined requirements met as built.
- **`PermissionsStage`** (`musaeus/stages/permissions.py`) — built
  2026-08-17 and **wired into `DEFAULT_PIPELINE`** at position 3 the same
  day (verified live 2026-08-21). The "deliberately standalone, matching
  the `GhostStage` precedent" framing in an earlier version of this
  bullet was a misreading of a console-menu note — corrected per §9.
  Covers the one real gap found when the old, archived `health.py` was
  compared against current `CorruptStage`/`PreflightStage`: Windows/ExFAT
  permission repair (644 files / 755 dirs) — corruption detection and
  metadata quality checks were both confirmed already fully covered
  elsewhere, with `CorruptStage` independently fixing the same
  stale-`file_path`-after-move bug class as §4.12, a third confirmed
  instance of that pattern. `dry_run()` reports without touching
  anything; `run()` re-scans live (never a stale snapshot) and chmods,
  logging `PERMISSION_FIXED` per change. Exercised against a real 3-file
  `INBOX` run 2026-08-18 — but all three already had correct
  permissions, so **the actual `chmod` repair path has still never
  executed against real dirty data**. **Grey confirmed 2026-08-21 that
  Windows/ExFAT intake is still a live workflow and the stage stays** —
  so it is retained by decision rather than by demonstrated use, which is
  the honest way to describe a guard that has never had to fire.
- **`hash_index.db`** (`ALAC-Library/_history/hash_index.db`) — the
  persistent cross-batch hash ledger (`finalized_hashes` table), which
  deliberately survives DB resets. **15,653 rows as of 2026-08-21**
  (measured directly; earlier figures of 11,800 / 11,815 were correct
  when taken — it legitimately grows, and the old "empty vs 15,084"
  contradiction was two checks at two different times, see §4.16). Read
  by `CrossDupeStage`, written by `FinalizeStage`, checked by
  `AuditStage`. Used at least once as the actual recovery mechanism for
  the 2026-08-14 data-loss incident.

  **Its append-only design has a serious failure mode, confirmed live on
  2026-08-21 — see §4.17.** The ledger is never pruned when a finalized
  file is later moved or removed, and `CrossDupeStage` treats an index
  hit as proof a copy is already held without verifying that the indexed
  path still exists. 14,045 of the 15,653 entries currently point at
  files that have since been moved into `DUPES_MOVED_FOR_REVIEW`, and in
  300/300 sampled cases the indexed "winner" path no longer exists on
  disk at all. Treat this file as a *hint* that a copy may exist, never
  as proof.
- **Archive** (still in design, not yet built as a formal pipeline
  feature) — an untouched, offline tier of ORIGINAL, pre-pipeline files,
  distinct from both `ALAC-Library` and any AAC export, and distinct
  from the transient `STAGING` buffer described above. **Not the same
  thing as `ALAC_Archive` below** — resolved 2026-08-18 after Claude(chat)
  flagged the ambiguity: the true untouched-original tier this bullet
  describes already exists informally as Grey's own manually-maintained
  external backup (`/media/grey/USB2/Curated.RAW.Files`, referenced in
  §5's canonicalize-reorder entry), not as a MUSAEUS-built feature —
  still genuinely unbuilt as far as MUSAEUS's own pipeline is concerned.
- **`ALAC_Archive`** (built and populated 2026-08-18) — a *different*,
  newer tier: pristine but already Phase-1-processed content (deduped,
  canonicalized, finalized — just not yet LUFS-baked), sitting ahead of
  `ALAC-Library` in the Phase 2A/2B flow. See §5's Phase 1/2A/2B/3
  roadmap entry for full detail. Currently holds all 1,385 of the
  vault's finalized rows, migrated via `scripts/musaeus_migrate_to_archive.py`.

## 3. Collaboration model

Three-way, deliberately structured:

- **Grey** — final authority on all policy and architecture decisions.
  Canon rules, keeper-selection tiebreakers, folder-routing exceptions,
  LUFS targets — all explicitly decided by Grey, never inferred by either
  AI.
- **Claude (this instance)** — reasoning, review, cross-checking Claude
  Code's work, handoff documentation. Does not have direct access to
  Grey's live systems.
- **Claude Code** — execution. Direct access to the real filesystem,
  database, and running processes. Reports findings back through Grey.

**Neither AI acts autonomously without Grey's oversight between them.**
This is a deliberate choice, not a current limitation to be automated
away — see the handoff's discussion of why a direct Claude-to-Claude-Code
bridge isn't being built.

## 4. Standard operating procedures

Hard-won this session specifically because violating them (or their
absence) caused real problems. Treat these as standing rules, not
situational judgment calls:

1. **One stage/operation at a time. Physically verify DB-vs-disk after
   each, before advancing.** Never trust a status summary alone — this
   caught the Finalize bug, the article-suffix triple-ownership bug, and
   the Car-Library wrapper's override-bypass bug, none of which were
   visible from reading code alone.
2. **Always maintain an untouched, pristine copy of original files before
   any irreversible processing step** — specifically before Canonicalize,
   which permanently deletes pre-conversion originals once its own
   staging copy and DB write are verified. This is Grey's explicit
   standing practice (confirmed: "why have raw files when you can clean
   them up in advance... saving a copy before the LUFS just makes
   sense") and should be treated as non-negotiable, not optional per
   batch.
3. **Fail-first regression testing.** A fix isn't verified until the test
   has been shown to fail *without* the fix, then pass *with* it. A test
   that passes either way proves nothing.
4. **Trace root cause before patching, especially for anything that's
   regressed more than once.** The article-suffix bug was "fixed" twice
   before the actual mechanism (two, then three, independent
   implementations of the same convention) was found and addressed
   properly.
5. **Never trust "I closed it" / "nothing else is running" without
   independent confirmation.** The Nemo window and the untriggered
   pipeline run both demonstrate this directly — a stated intention and a
   confirmed outcome are different things. The specific Nemo case:
   thumbnailing `FORGE2TB` in the file manager caused sustained ~36MB/s
   disk writes that blocked `musaeus status` queries — closing the Nemo
   *window* didn't stop it, since the thumbnailing process kept running
   detached; it had to be killed by PID directly. Treat "close the
   window" and "the process is gone" as two different claims needing two
   different checks.
6. **Verify claims against primary sources, not secondhand or
   adjacent-but-different ones.** The AI-generated ReplayGain summary
   citing sources about entirely different products (a CD player, a
   different manufacturer's streamer) looked authoritative and wasn't —
   the actual device manual, fully searched, was the source that
   resolved it.
7. **Dry-run before any DB-mutating change** — with the known caveat that
   `--dry-run` currently still creates real directories and DB/event
   records before any stage runs (tracked as `P0-02`/`P0-04`/`P0-05` in
   `.kiro/specs/musaeus-consumer-readiness/tasks.md`; not yet fixed). Live-run-then-
   verify is the current working substitute. As of `P0-02`, at least 32
   commands now fail closed (exit code 2) rather than silently running
   as a fake dry-run — but that's a guard against *using* the broken
   behavior, not a fix for the underlying bug itself.
8. **On any recovery/reconciliation operation: restore first, then
   rebuild indexes, then reprocess** — never bake stale state into
   rebuilt indexes.
9. **A snapshot-in-time process check answers "is anything running right
   now," not "did anything run since I last looked."** Don't conflate the
   two — this gap is exactly what let a full pipeline run go unnoticed
   until its results were found after the fact.
10. **Automated code-review/self-heal tooling must be report-only —
    never auto-commit.** Confirmed real failure mode, not hypothetical:
    the `orpheus_overnight_self_heal.py` cron job (previously 1:30 AM)
    auto-committed fixes from a free-tier model with commit messages that
    were literally raw code snippets, twice left the repo checked out on
    a stray `review/*` branch mid-session (once possibly on `main`,
    unconfirmed), and crashed on its own regex bug (`re.error: bad
    escape`) after already committing changes to `curator.py`. Standing
    decision: findings get written to a file for morning review; Grey
    accepts, edits, or rejects each one by hand; nothing is committed
    automatically. The job is confirmed disabled, pending a report-only
    rebuild before it's re-enabled. Leftover `review/2026-08-03` through
    `review/2026-08-10` branches from it are still sitting unmerged (tracked in
    `MUSAEUS_OPEN_ITEMS.md`).
11. **Long-running processes must guard against concurrent DB writers
    before starting — don't rely on `busy_timeout` alone.** A 10-second
    `busy_timeout` cannot cover a collision with a process that holds the
    DB open for hours. Confirmed failure mode: an overnight cron run
    scored 0 passed / 4 failed, every top-level stage hitting
    `sqlite3.OperationalError: database is locked` within 10 seconds of a
    live process already holding the DB. Fix: a `pgrep`-based
    concurrency check (catching both the installed `bin/musaeus` entry
    point and direct `python3 -m musaeus` invocations) that skips the run
    cleanly, logs a grep-able marker with the offending PID/cmdline, and
    exits 0 instead of attempting the run.
12. **Collision-safe DB writes: disk-side change first, DB update by
    rowid second.** On a `sqlite3.IntegrityError` (typically a `UNIQUE`
    collision on `archive.file_path`), revert or quarantine the
    disk-side change and leave the pre-operation source completely
    untouched — never lose track of a file because a write failed
    partway. Originated in `organize.py`'s `_apply_rename`; now the
    standard pattern, also applied in `canonicalize.py` and
    `finalize.py`.
13. **Destructive resets require an interactive TTY and an explicit
    typed confirmation — no automated path exists.** Confirmed by reading
    both code paths in full: `cli.py`'s `_cmd_reset()` (requires an
    interactive TTY, must type `RESET`) and `console.py`'s TUI "Hard
    reset" (requires typing `DELETE` twice). Both only delete
    `musaeus.db`/`-wal`/`-shm` — no `rmtree`, `INBOX`/`ALAC-Library`
    untouched either way. No cron job, script, or AI tool call (Claude or
    Claude Code) can trigger either path non-interactively. This is also
    why a hard reset should always be treated as a deliberate human
    action, not something to investigate as a possible bug or accident
    by default.
14. **A reconciliation tool that sweeps its whole table unconditionally
    cannot be safely reused for a targeted/partial fix — verify its
    actual query scope before running it, don't assume it can be
    parameterized.** Confirmed directly: `GhostStage` marks
    everything currently missing on disk as `GHOST`, full-table, no ID
    filter available. During the 2026-08-14 recovery, running it as-is
    would have correctly caught 4,608 genuinely-gone rows but also
    incorrectly re-marked 10,644 rows that had just been recovered
    moments earlier — a real near-miss, caught by checking scope with a
    read-only preview first, not by trusting the tool's name. The actual
    fix used a small scoped one-off mirroring `GhostStage`'s exact
    `status`/`event` shape but restricted to a specific, independently-
    derived ID list — same pattern as §4.12's collision-safe writes:
    don't trust a general-purpose tool's blast radius, verify it.
15. **Never use the bare word "canon" as shorthand when writing about
    pipeline stages or ordering — always name the specific stage.** This
    project has two unrelated things both called "canon" in casual
    prose: `ArtistConsolidateStage` (artist-name canon, Act 1) and
    `CanonicalizeStage` (audio-format canon/conversion, Act 3). "Canon
    runs before dedupe" is true for the first, false for the second —
    Act 2 (dedupe) runs entirely before Act 3 (`CanonicalizeStage`).
    Confirmed as a real, not hypothetical, misreading risk (§2's
    pipeline-stage-order entry). The class names themselves are already
    unambiguous; the trap only exists in shorthand prose, so the fix is
    a writing discipline, not a rename.
16. **A "resolved" finding isn't actually resolved until it's folded back
    into this doc — confirming something in a session isn't the same as
    updating the doc that describes it.** Confirmed pattern, four
    separate instances now, not a one-off: `neardupe.py`'s
    `TITLE_THRESHOLD` and `progress.py`'s git-tracking status both sat
    re-flagged as open across multiple passes after already being fixed;
    the API-key-entry menu was carried as "not confirmed built" after it
    was already built and wired into `console.py`; and §2's own
    pipeline-stage-order entry described `Permissions`/`Enrich`/
    `MBEnrich` as on-demand-only for about a day after they'd already
    joined `DEFAULT_PIPELINE`. None were individually costly, but the
    doc's own job is to be the trustworthy current-state reference —
    every one of these was a session ending with real code changed and
    verified, but the doc not updated in the same pass. Treat "confirmed
    in this session" and "written into the doc" as two separate steps,
    not one — the second doesn't happen automatically just because the
    first did.
17. **A hash ledger is evidence that a file was *seen*, never evidence
    that a copy is *held*. Verify the twin exists on disk before acting
    on a match.** Confirmed live 2026-08-21, and it had already cost
    ~10,600 tracks by the time it was found. `hash_index.db` is
    append-only by design (it must survive DB resets — that is the whole
    point of it), but nothing prunes an entry when the file it names is
    later moved. `CrossDupeStage` read a hit as "we already have this"
    and quarantined the incoming file. Where the indexed file had itself
    been moved into `DUPES_MOVED_FOR_REVIEW`, the ledger entry outlived
    it and the next pass quarantined the file **as a duplicate of
    itself**. Measured: 14,045 of 15,653 ledger entries name a file now
    in quarantine; 300/300 sampled indexed paths no longer exist on
    disk; 10,597 quarantined files are the only copy of their audio in
    the entire database.

    The failure is quiet in the worst way — every individual decision
    looks correct in the log, the counts look like a big successful
    dedupe run, and the library shrinks to 1,503 files while reporting
    success. Nothing was deleted, which is the only reason this is
    recoverable.

    The general rule, beyond this one bug: **an index is a cache of a
    claim about the filesystem, and a cache that is never invalidated
    will eventually lie.** Any lookup whose answer authorises a
    destructive or relocating action must re-confirm against the
    filesystem before acting — the same discipline §4.12 already
    requires for `file_path` after a move, and the fourth distinct
    instance of that bug class.

18. **A verification must be falsifiable, and you must prove it can
    fail.** Added 2026-08-23 after writing three checks that could never
    have fired. One called `getattr(law, "genres", set())` on an
    attribute that did not exist — the default swallowed it and the check
    silently passed forever. One read `probe.get("codec_name")` from a
    document where the codec lives on a nested stream — always `None`.
    One compared through a helper that folds case, so the very variant it
    was meant to catch was absorbed as valid.

    All three were written *as part of the anti-silent-no-op work*, which
    is the point: the failure mode reproduces inside its own cure. The
    discipline is §4.3 applied to assertions themselves — before trusting
    a check, feed it the bad input and watch it fail. A check that cannot
    fail is worse than no check, because it converts an unknown into a
    false reassurance. "100% coverage, 0 strays" was reported truthfully
    by an instrument that was not looking.

    Its structural cousin: **a vocabulary derived from its own data
    cannot reject a typo in that data.** `GenreLaw.genres` is
    `set(self._map.values())`, so `Gwen Stefani,pop` did not fail
    validation — it *became* a legal genre. Any closed set inferred from
    the file it is meant to police has this property.

19. **Sync runs from the source of decisions outward, and you measure
    before you sync.** Added 2026-08-23. The library holds the owner's
    genre decisions; `MasterLaw.csv` is derived and follows it. The
    obvious repair — normalise the law's artist names so its dormant
    rules start matching — was measured before being written and would
    have **overwritten 76 freshly-made decisions**, most of them back
    into the catch-all bucket that had just been emptied.

    Nothing about the operation looked destructive: it only corrected
    spellings. The damage would have come from what those corrections
    *activated*. Before any normalisation that changes how keys match,
    compute the resulting diff against live data and read it — the blast
    radius of a key change is every rule that starts or stops firing, not
    the rows you edited.

20. **Bound the work; do not rely on a timeout to rescue you.** Added
    2026-08-23 after a 12-hour audio file OOM-killed a full pipeline run
    at the BPM stage, discarding 18 stages of completed work with no
    checkpoint. `MonoLoader` materialises an entire decoded file as
    float32 (7.6 GB here) before anything downstream can trim it.

    A timeout could not have helped: Essentia runs in C++, and a Python
    signal handler only executes between bytecodes, so `signal.alarm()`
    would not fire until the call it was meant to interrupt had already
    returned. **Work that happens inside a C extension is uninterruptible
    from Python — it has to be bounded at the input.** BPM now refuses
    anything past a duration ceiling *before* decoding, and analyses a
    centred window above a threshold.

    Second lesson from the same incident, and the more portable one:
    **the run reported success.** The wrapper's own exit status was 0
    while the process had died on SIGKILL (137). The only reliable
    symptom was a missing terminal event. Do not read a shell wrapper's
    exit code as the exit code of what it wrapped, and confirm a run
    finished by its terminal event, not by absence of an error.

    **Corrected 2026-08-24: that terminal event is `RUN_END`, not
    `RUN_COMPLETE`.** This SOP, three handoffs and both working documents
    all named `RUN_COMPLETE`, which this codebase has never emitted —
    `musaeus/` emits exactly `RUN_START` and `RUN_END`. So the rule as
    written could not fail: a search for `RUN_COMPLETE` returns nothing
    for a clean run and nothing for a killed one, and every use of it
    "confirmed" the same answer. `RUN_END` also carries the outcome in
    its note (`success=True stages=24`), which is the thing actually
    worth reading. **A verification step that names the wrong identifier
    is the §4.18 failure in prose rather than in code** — and it survived
    precisely because it was only ever used to confirm a run that had in
    fact failed.

21. **A field being populated is not the field being right.** Added
    2026-08-24. `year` is populated on 99.9% of catalogued tracks, and
    that number was used — in this project's own argument, by me — to
    conclude that era information was already held and did not need
    encoding anywhere else. Measured against the library, `year` is the
    year of the **edition on disk**, not of the recording. 221 of 384
    Rock & Roll tracks and 284 of 559 Blues tracks carry a year of 2010
    or later. The Beach Boys' *409* (1962) is dated 2012. The Ad Libs'
    *The Boy From New York City* (1964) is dated 2022 and says
    "(Remastered 2012)" in its own title.

    Coverage was measured; meaning never was. Anything reasoning about
    age from that field inherits the reissue date and is confidently
    wrong — an era playlist, a Classic/Modern genre split, a sort by
    year. **Before trusting a field, verify what it means on a row you
    can check independently, not just how many rows have one.** A
    suspiciously complete number deserves the same suspicion as a
    suspiciously perfect one (§4.18).

    `original_year` now holds the recording's first release date,
    recovered from MusicBrainz and stored **alongside** `year` rather
    than over it: the edition year is a real fact about the file, and
    overwriting would leave no way to tell a corrected row from an
    unchecked one.

22. **A high match score is not identity.** Added 2026-08-24.
    `MBEnrichStage` accepted any MusicBrainz artist scoring 85 or better
    and wrote its MBID. MusicBrainz scores a *containing* name at 100,
    so it wrote `Red` → Red Hot Chili Peppers, `Little Feat` → Little
    Richard, `Dion` → Céline Dion, `Jan & Dean` → Jan Arnald,
    `Simon & Garfunkel` → Simon Jäger. 27 rows across 16 artists, each
    then stamped `mb_enriched_at` — so every corrupted row marked itself
    finished and would never have been revisited.

    **A ranking function answers "which of these is closest to the
    query". It never answers "is this the same thing."** The identity
    check has to be separate, explicit, and able to reject a perfect
    score. Names are compared after article folding, so the library's
    `Beatles, The` still matches MusicBrainz's `The Beatles`.

    Found alongside it, and the reason those matches were so bad: the
    Lucene query was URL-encoded twice, so `Dusty Springfield` went out
    as the literal term `Dusty%20Springfield` and matched nothing.
    **Every artist name containing a space had been failing, silently,
    for as long as the file existed** — single-word names worked, which
    is exactly why it survived: it failed on almost everything while
    appearing to work on whatever was spot-checked. Silent no-op number
    nine.

23. **A decision is not applied until every file that encodes it
    agrees.** Added 2026-08-24. `Pop, Rock` was retired on the 23rd:
    drained from 3,004 tracks to 0, removed from MasterLaw, its 192
    refiling rules rewritten, and recorded as closed in three separate
    documents. It was still a legal genre. `Genre_Allowed.txt` — the
    closed vocabulary `GenreCanon` actually loads — still listed it, and
    `Genre_Canonical_Map.txt` still held five rules pointing at it,
    including `Pop Rock` and `power pop`. The next ingest tagged
    "power pop" would have started refilling the bucket.

    The same audit found ten further rules aimed at values that were not
    in the vocabulary at all (`Soundtracks`, `R&B, Funk, Soul`,
    `Rock N'Roll`, `Rock Blues`, `Baroque`), and `resolve()` returns a
    map target without checking that it is legal — so the canon could
    emit a genre the canon forbids. **When a decision lives in more than
    one file, closing it means enumerating those files and checking each
    one. The count of rows affected is not the check.**

24. **A stored hash describes the audio as it was then, not as it is
    now.** Added 2026-08-24. `audio_hash` is a PCM hash: stable across
    re-tagging and container rewriting, which is exactly why it is the
    project's identity for a recording. But **LUFS baking alters the
    audio**, so every hash recorded before a bake describes something
    that no longer exists on disk. Measured: three sampled `lufs_baked_at`
    files all hash differently now than the value stored for them, while
    unbaked files match exactly.

    This is not a defect to fix — baking is deliberate and the old hash
    was true when written. It is a property to design around, and it has
    already produced two live consequences. The finalized-hash ledger
    naturally accumulates entries that no longer match any file, which
    is one reason a stale-path count is normal rather than alarming. And
    a deny-list seeded from that ledger holds **pre-bake** hashes, so it
    catches a fresh re-download (unbaked, and therefore matching) but not
    the baked copy pulled out of the library — which is how the two
    most recently deleted files were missing from a deny-list built
    specifically to hold them.

    The general rule: **any content-addressed index over content that
    some stage rewrites decays silently, and the decay is invisible from
    inside the index.** Nothing in the ledger looked wrong. Deciding what
    a stored hash is a hash *of* has to be part of using it, and where
    both matter, both have to be recorded.

25. **A file move is not transactional; a database write is. Write
    first, move second, commit third.** Added 2026-08-24, at the cost of
    two destroyed files.

    A script moved each file with `shutil.move` and then wrote its row,
    committing every 25. Two recordings resolved to the same destination
    filename — `shutil.move` **overwrites** an existing target, so the
    first was destroyed — and the resulting UNIQUE constraint error then
    rolled the database back while the filesystem kept **all 86 of its
    moves**. The library was left with rows pointing at paths that had
    moved out from under them. Recovery meant re-deriving every
    destination by hand; the two overwritten files came back only from
    the USB2 mirror verified earlier that morning.

    Both halves of the fix matter and neither is sufficient alone.
    `unique_path()` — which already existed in `organize.py` for exactly
    this, and which `VariousArtistsFixStage` already used — makes a
    collision impossible. Ordering the DB write before the move makes a
    failure recoverable: the write can be rolled back, the move cannot.
    And the commit must be per row, because `rollback()` discards
    everything uncommitted — with a batched commit, one failure undoes
    twenty-five rows whose files have already moved.

    The general rule: **when an operation spans two systems and only one
    of them can be undone, do the undoable one first.** `ForgeStage` and
    `VariousArtistsFixStage` were both audited and corrected against this
    the same day.

26. **A stage that could destroy the library is not safe because nobody
    runs it.** Added 2026-08-24. `OrganizeStage` selects
    `status='CATALOGUED'` rows and builds every target path under
    `ctx.inbox`. Measured against the live vault: running it moves
    **10,660 of 10,660 catalogued files out of ALAC-Library into the
    INBOX**, where the pipeline would treat the entire finalized library
    as new arrivals. It is coherent for the job it documents — tidying
    loose files that are still *in* the inbox — and dangerous only
    because nothing restricts it to that case.

    Its absence from `DEFAULT_PIPELINE` is the only thing that has been
    protecting the library, and absence is not a safeguard: it is a fact
    about today's configuration that any future edit can change. It was
    very nearly run on a recommendation built from its docstring rather
    than its code. **Read where a stage writes before believing what it
    says it does, and compute the blast radius before running anything
    that moves files in bulk.**

27. **A verification must check what the row CLAIMED, not a constant.**
    Added 2026-09-03.

    `canonicalize.verify_effect` asserted "the codec must be ALAC" for every
    sampled row. The `CANONICALIZE` event is logged for all three outcomes and
    two of them are not ALAC by definition — PASSTHROUGH may be ALAC *or* AAC
    (it means "already canonical"), and TRANSCODED must be AAC. Five correctly
    untouched AAC files were reported as `EFFECT NOT VERIFIED`, and the stage
    sealed ✗UNVERIFIED for doing exactly the right thing.

    Writing the test found two further faults in the same line, neither of
    which had appeared in any run log: TRANSCODED was flagged too, so every
    correct 256k export would trip the check depending only on which rows the
    twelve-row sample happened to hit; and a TRANSCODED row that came out ALAC
    was **not** flagged, because ALAC satisfied the constant. Two
    false-positive classes and one blind spot, all from asserting a value
    instead of the claim.

    This is the 2026-09-01 honesty fix inverted. That one stopped `✓verified`
    meaning "I looked and found nothing" when nothing had been looked at; this
    one stops `✗UNVERIFIED` landing on work that was correct. **A seal that
    cries wolf is discarded as fast as one that lies**, and after 2026-09-08
    the seal is the only signal anyone reads.

28. **Every path component is capped at 255 BYTES, and truncation is on
    encoded bytes.** Added 2026-09-03, at the cost of an aborted dedupe.

    `DupeResolverStage` died with `OSError 36` building a directory from a
    388-byte 24-artist credit. Linux caps each path *component* at 255 bytes
    regardless of how long the whole path is; nothing in `organize.py`
    enforced that, and six stages build paths through its
    `sanitize_path_component` / `build_track_filename` helpers. `s[:255]`
    counts codepoints, not bytes, so it still overflows for any non-ASCII
    name — encode, truncate, decode with `errors="ignore"`. The join is what
    the filesystem sees, so `"Artist - Title.ext"` needs its own budget: two
    separately capped halves still make a 513-byte filename.

    The same crash carried a second, independent fault. `_move_losers` had
    isolated per-item `OSError`s from the move since it was written, but
    `_target_path` sat one line **above** that guard while doing filesystem
    work of its own. One unplaceable file therefore aborted every remaining
    move and left the dedupe half done. **A retry loop would not have helped:
    the failure is deterministic, so a second and third attempt fail
    identically.** Isolation is what a per-item failure needs, and the stage
    already had the shape for it.

29. **A wanted-list entry must survive the check "do I already own this?"**
    Added 2026-09-03.

    The hasher is the only thing in the pipeline that decodes every file end
    to end — it must, to derive `audio_hash` from PCM — so a `HasherError` is
    the strongest evidence MUSAEUS ever gets that a file is unplayable. That
    evidence used to stop at a log line: the row stayed PENDING, never reached
    Scholar, and nothing recorded that a replacement was wanted. It now writes
    a `TuneMyMusic.csv` line, reading tags off the damaged container because a
    hash-failed row never gets metadata from Scholar.

    `CorruptStage` cannot cover this. It decides on a filesize-versus-duration
    ratio, and in the 2026-09-03 run it quarantined ten files and caught none
    of the four undecodable ones. **Size prioritises; only a decode decides.**

    The `already_owned()` guard is what stops the list becoming noise. David
    Bowie's "Cat People" failed to decode in one copy — 2 MB, header claiming
    5:10, decoding 13 seconds — while a clean 53 MB master sat in the library
    the whole time. Asking for a record that is already owned is how a useful
    list becomes one nobody reads.

    **Note the limit honestly:** this catches *hard* decode failures only.
    Partially truncated files decode successfully on their fragment, so the
    hasher never raises and they never reach the list. Four such FLACs were
    caught in the same run by Canonicalize's duration check instead.

30. **A check that validates a transformation cannot see what was never
    transformed.** Added 2026-09-04, from a file that got past a working
    refusal.

    Canonicalize's duration check compares a conversion's OUTPUT against its
    source. `PASSTHROUGH` means "already ALAC-in-.m4a or already AAC-in-.m4a,
    nothing to convert, **no file write at all**" — so a passthrough produces
    no output, and the check does not run. Not a bug in the check; a hole in
    what it can reach.

    Carlos Santana's "Bella" was in INBOX twice, as the same truncated audio:
    a FLAC, which was converted, checked and correctly REFUSED, and an
    already-ALAC copy, which passed straight through unexamined and was
    finalized into the library. The refusal worked perfectly. Its twin walked
    around it.

    **There is no per-file test for this.** Header, stream and full decode of
    the escaped file all agree at 143.78 s with no ffmpeg error. It is a
    valid short file. "Decodes clean" is not a completeness check, and a
    re-encode OF truncated audio decodes perfectly — so where completeness
    matters, compare DURATION against a known-good copy, or compare the PCM
    hash.

    **The corollary about choosing a signal.** The first check written for
    this compared durations between copies of the same recording. Measured
    against the live library it produced **802 findings**: a live take runs
    two or three times its studio original, so it reported studio masters as
    truncated, and 544 of the 802 sat in the 60-90% band ordinary edits
    occupy. The real case is 53.5% — inside radio-edit territory — so no
    threshold separates them. It was abandoned rather than tuned. **When a
    heuristic cannot separate the real case from legitimate variation, do not
    pick a threshold; find an exact signal.** Here that was the rejected
    file's `audio_hash`: any other row carrying it holds the same refused
    audio. That fires once on the whole library, on the one file that
    escaped. `doctor` now runs it (§2, "rejected audio present anyway").

    Measure a new check against the real library before trusting it. Both
    faults above — the 802 and, in the replacement, four false hits that were
    the refused sources matching their own hash — were found by running it,
    not by reading it.

## 5. Confirmed canonical conventions

- **Article-suffix format: `", The"`** (comma-suffix, e.g. `Beatles,
  The`) — confirmed via on-disk evidence (341 real folders vs. zero using
  any other format), matches ORPHEUS's own convention, and matches real
  music-app sorting behavior. `(the)`/`(The)` parenthetical forms are
  incorrect and were the source of a three-times-regressed bug this
  session.
- **15+ articles across languages** need case-insensitive matching and
  uniform capitalization when moved to suffix position (`The, A, An, Le,
  La, Les, El, Los, Las, De, Het, Een, Die, Das, Ein, Eine`) — but a
  **protected-names guard is mandatory** for real band/artist names that
  happen to start with an article word (`De La Soul`, `Los Lobos`, `Los
  Bravos`, `El Gran Combo`, etc.) — confirmed via real, live data
  corruption this session when this guard didn't exist.
- **Guest-credit stripping**: `with / feat. / featuring / special guest /
  and Friends` — strip the clause and everything after, then standardize
  remaining `and` → `&`. **Correction (2026-08-17):** this convention was
  only ever a discussed design until today — zero implementation existed
  anywhere in git history before this session (no `build_canon()`
  function exists at all; that name was a mistaken assumption in earlier
  discussion). Implemented today directly in `artist_consolidate.py`,
  alongside two newly-confirmed bugs found in the same investigation and
  fixed in the same change: (1) `_normalize_key()` stripped the
  parenthetical `(the)` form but never a leading `the ` word, so e.g.
  `Chieftains`/`the Chieftains` were never grouped as the same artist at
  all — the actual, confirmed cause of a real 76/78-track split still
  present in the live DB; (2) `_strip_collaborator_tail()`'s comma-split
  heuristic (`if "," in raw and "&" not in raw: split on first comma`)
  can't distinguish a legitimate `", The"` article suffix from a real
  collaborator credit — confirmed dormant (312 real `, The` artists
  cross-referenced, zero live collisions triggered yet) but a ticking
  bug that would silently merge the wrong two artists the moment one
  does collide. **The identical bug is independently duplicated in
  `curator.py`'s `_primary_artist()`** (car-export folder naming) — and
  that copy **is** live-reachable today, no collision needed, since any
  car-export run touching a `, The` artist already produces a wrong
  folder name. Fixed in both files in the same change (commit `c559fa6`,
  same session as the guest-clause implementation, commit `3337537` for
  an unrelated DupeResolver fix landed just before it). Fix logic: only
  treat a comma-tail as strippable if it's not one of `the`/`a`/`an`
  (case-insensitive) — anything else is a real collaborator credit.
- **Prefer-solo-name** is the general folder-routing policy for
  backing-band billing (e.g. `Bob Seger & the Silver Bullet Band` →
  `Bob Seger`), with named exceptions: `Simon & Garfunkel`, `David &
  David`, CSN, CSNY.
- **LUFS targets**: `ALAC-Library` at **-18 LUFS**, `AAC-Car` export at
  **-14 LUFS**. Both need to be physically applied to audio (not just
  written as ReplayGain tags) — confirmed via the AVR-X1700H's full
  owner's manual (304 pages, zero mentions of ReplayGain) that the home
  AV receiver cannot read RG tags at all. The car head unit (Neutron
  Player) does support RG, but the AVR is the binding constraint.
- **Three-tier structure** (in design): **Archive** (untouched original,
  offline, no naming/LUFS changes) → **ALAC-Library** (normalized names,
  -18 LUFS baked in) → **AAC-Car** (encoded and masked *from Archive*,
  not from ALAC-Library, to avoid stacking two loudness transforms on top
  of each other — -14 LUFS baked in). Note: `ALAC_Archive` (built
  2026-08-18 — see §2) sits between the untouched-original **Archive**
  and `ALAC-Library` in the actually-implemented flow: Archive
  (untouched, offline) → `ALAC_Archive` (Phase-1-processed, not yet
  LUFS-baked) → `ALAC-Library` (LUFS-baked production copy) → AAC-Car
  (encoded from `ALAC_Archive`, -14 LUFS). The three-tier model above
  remains the design target; `ALAC_Archive` is its Phase-2A
  implementation detail, not a fourth permanent tier — but it is real on
  disk, read by live code, and as of 2026-08-21 it is what
  `rebuild_from_disk.py` hashes to preserve `audio_hash` continuity, so
  it is named here rather than left an invisible gap between tiers.
- **`STAGING` write-then-verify flow for Canonicalize → Finalize**
  (confirmed working, 2026-08-11 session): `CanonicalizeStage`'s
  CONVERT/TRANSCODE output writes to `STAGING/<row.id>_<name>.m4a`, never
  as a sibling file in `INBOX`. It's ffprobe-verified there (stream-count
  + duration check against the still-untouched `INBOX` source) before the
  DB row is updated by rowid (§4.12's collision-safe pattern); only once
  that DB write is confirmed does the original `INBOX` source get
  deleted. A failed verification renames the attempt to
  `STAGING/<row.id>_<name>.m4a.FAILED_VERIFY` and logs a
  `CANONICALIZE_VERIFY_FAILED` event — never silently deleted or
  retried; a DB collision logs `CANONICALIZE_DB_COLLISION` and leaves
  the staged file for manual review, original untouched either way.
  `FinalizeStage` is source-agnostic (`STAGING` for CONVERTED/TRANSCODED
  rows, still `INBOX` for PASSTHROUGH rows) and moves whichever into
  `ALAC-Library`, deleting the source only after that move and its own
  DB write are confirmed (`FINALIZE_DB_COLLISION` on failure, same
  revert-and-leave-untouched pattern). **`STAGING` should be empty at
  the end of any clean run — anything left there is itself a signal to
  check manually, not something to auto-clean.** This is distinct from
  the three-tier structure above: `STAGING` is a transient per-file
  buffer inside one stage handoff, not a fourth permanent tier. Note:
  this flow covers the `archive.file_path`/`finalized_at` DB row — it
  does **not** currently extend to the separate persistent hash-index
  write, which remains a known open gap — tracked in
  `MUSAEUS_OPEN_ITEMS.md`.
- **Lossy-source policy** (confirmed directly by Grey, 2026-08-17): all
  files converge on `.m4a` in `ALAC-Library` — lossless sources go
  through `CanonicalizeStage`'s CONVERT path to real ALAC, sub-lossless
  sources go through its TRANSCODE path to AAC, both land in
  `ALAC-Library` as `.m4a` (matches §2's `CanonicalizeStage` component
  description). Sub-lossless files are **not** withheld from the
  library — they're converted and logged to `TuneMyMusic.csv` at the
  same time, so a better/lossless source can be manually re-sourced
  later without blocking the file's presence in the library now.
  Correction: an earlier draft of this convention (proposed by Claude in
  chat, not yet written here) had this backwards — claiming sub-lossless
  sources are withheld from `ALAC-Library` entirely. That contradicted
  both this doc's own §2 and `Notes to myself.md`'s direct description
  of `TuneMyMusic.csv`'s purpose, and was caught before being added.
- **Phase 1 pipeline reorder** (Grey's explicit call, 2026-08-17,
  implemented same day — `musaeus/stages/__init__.py`): `PermissionsStage`
  joined Act 1 (right after Ingest) and `EnrichStage`/`MBEnrichStage`
  joined as default-on (positioned last, after Audit — deliberately
  isolated from the file-safety-critical stages so a Last.fm/MusicBrainz
  network hiccup can't block them). Both stand.
  **BPM and VariousArtistsFix wired in, 2026-08-19/20 (Grey's explicit
  call, commit `a0e7265`), current order:**
  `Preflight → Ingest → Permissions → Sentinel → Scholar → Health →
  Corrupt → AlbumArt → Normalize → Sanitize → ArtistConsolidate →
  VariousArtistsFix → CrossDupe → NearDupe → DupeResolver → Canonicalize →
  Finalize → BPM → Forge → Tagger → Audit → Enrich → MBEnrich`.
  Both were built standalone first, then wired in the same night once a
  full USB2 backlog run (§9) made a real cost/placement decision
  possible rather than a hypothetical one. BPM: positioned right after
  Finalize, near Forge, per Grey's original request — an initial
  cost objection (Essentia is heavy) was withdrawn once Grey pointed out
  BPM's own tag-read-first shortcut plus `bpm_analyzed_at` resumability
  mean that cost is paid once per new file, ever, not on every pipeline
  run. VariousArtistsFix: positioned at the end of Act 1, right after
  ArtistConsolidate — resolving the real artist before Act 2's dedup
  runs means CrossDupe/NearDupe see it instead of a shared "Various
  Artists" tag on every candidate row, the same logic that already put
  ArtistConsolidate ahead of dedup. MusicBrainz lookups are forced off
  (`various_artists_no_mb=True`) specifically when run as part of
  `DEFAULT_PIPELINE` via `run`/`dry-run`, so a network hiccup early in
  Act 1 can't stall an otherwise file-safety-critical automatic run —
  bracket/filename-segment resolution still runs; `musaeus
  various-artists-fix` invoked standalone still defaults to MB lookups
  on.
  **Canonicalize-before-dedupe reversal — tried 2026-08-17, REVERTED
  2026-08-18.** The 2026-08-17 pass also moved `CanonicalizeStage` ahead
  of dedup, accepting a real tradeoff (a sub-lossless duplicate-loser
  would lose its pristine INBOX original to a wasted TRANSCODE before
  dedup could flag it — mitigated at the time by the confirmed
  `/media/grey/USB2/Curated.RAW.Files` backup). Claude(chat) challenged
  the motivating case the next day: the reversal only pays for itself if
  `CrossDupeStage` can't already catch cross-format duplicates (e.g. a
  FLAC and its future ALAC-in-`.m4a` twin) without Canonicalize running
  first. Claude Code checked directly — `sentinel.py`'s own docstring:
  "Same audio, different container: audio_hash matches → EXACT
  duplicate." `audio_hash` is a PCM-stream hash, not a container hash, so
  cross-format duplicates already matched fine before the reversal ever
  existed. The reversal was paying real ffmpeg cost for a correctness
  problem that didn't exist. Reverted on Grey's instruction once this was
  confirmed — Canonicalize is back in Act 3, its original position, and
  the pristine-original tradeoff no longer applies.
  **Enrichment dry-run gap — also fixed 2026-08-18.** Claude(chat)
  separately flagged that folding Enrich/MBEnrich into the default-on
  chain made their pre-existing "dry_run still makes the real network
  call" gap (P0-03+, previously lower-stakes since both were on-demand
  only) worth actually closing rather than just tracking. Both stages'
  `dry_run()` now skip the real Last.fm/MusicBrainz call entirely — not
  just the DB write — reporting how many artists *would* be queried in a
  real run instead of actually querying them.
  **Bugs found and fixed while wiring the original reorder in:**
  `EnrichStage` and `NearDupeStage` (the latter pre-existing, just never
  exercised via the full `DEFAULT_PIPELINE` before now) both called the
  module-level `get_config()` singleton directly instead of using the
  already-injected `ctx.config` — invisible until a real disposable-vault
  test exercised them inside the full pipeline. Both fixed to use
  `ctx.config`. `MBEnrichStage.validate()` used to hard-fail the whole
  run on an unreachable network; fixed to degrade gracefully (skip +
  report, matching `EnrichStage`'s existing missing-API-key pattern)
  since it's no longer purely on-demand. Test coverage:
  `tests/test_pipeline_order.py` (locks in the current order, updated
  2026-08-18 for the revert), `tests/test_p0_01_characterization.py`/
  `tests/test_disposable_vault.py` (updated for both the MBEnrich
  graceful-degradation behavior and the dry-run-gap fix). Full suite:
  390/390 passing throughout.
- **Phase 1/2A/2B/3 roadmap** (Grey's terminology, distinct from the
  code's own Act 1/2/3 pipeline-execution grouping — see §4.15): Phase 1
  is the existing pipeline above (intake through dedupe/canonicalize/
  finalize/enrich). Phase 2A bakes -18 LUFS into `ALAC-Library`; Phase 2B
  bakes -14 LUFS (+ optional masking) into `AAC-Car`; Phase 3 is USB
  transfer/wipe (built 2026-08-18, see below). A new pristine, unbaked tier, `ALAC_Archive`,
  sits ahead of `ALAC-Library` — Phase 1 lands new work there going
  forward, Phases 2A/2B read from it. Existing `ALAC-Library` content
  migrates via `scripts/musaeus_migrate_to_archive.py` (2026-08-18,
  direct move-and-relink, sidesteps INBOX/dedup entirely since
  `hash_index.db` already has these files' `audio_hash` — see §4.12 for
  why re-ingesting would falsely quarantine them as dupes). Dry-run by
  default, smoke-tested against an isolated scratch vault, deliberately
  not yet run against the real vault (pending Phase 1 settling + the live
  overnight run finishing).
  **Build shape — corrected 2026-08-18, second Claude Code session:**
  neither Phase 2A nor Phase 2B is a `BaseStage` subclass wired into
  `DEFAULT_PIPELINE`. Both are standalone scripts under `scripts/`,
  matching `Car-Library`'s existing precedent (§2) of staying independent
  of `musaeus run`/cron. Phase 2B extends the existing, already
  execution-verified `scripts/car_library/build_car_library.py` /
  `build_aac_library.py` toolchain — its one real gap (tracked in
  `MUSAEUS_OPEN_ITEMS.md`) is a defined-but-dead `-14.0` LUFS target that
  needs wiring up, not a new tool. Phase 2A has no existing ALAC-side
  equivalent to extend, so it's new standalone-script work following the
  same shape. The original handoff brief's instruction to build fresh
  `BaseStage` subclasses (DB-column-tracked, `canonicalize.py`-style) for
  both halves turned out not to match either — caught before it was
  built, not after.
  **Phase 2A/2B built, 2026-08-18 (second Claude Code session, committed
  via this thread — c25240e):** `scripts/alac_library/build_alac_library.py`
  (new, Phase 2A) — DB-row-driven, reads `archive` rows whose `file_path`
  is under `ALAC_Archive`, same two-pass EBU R128 loudnorm + verify-then-
  atomic-swap + collision-safe-DB-write discipline as
  `migrate_to_archive.py`/`canonicalize.py`. `ALAC_Archive` still isn't
  wired into `config.py`, so the archive dir resolves via
  `--archive-dir`/`MUSAEUS_ALAC_ARCHIVE` for now. Two new nullable-timestamp
  columns, `archive.lufs_baked_at` / `lufs_baked_target`, added to
  `musaeus/db.py`'s migration list (no collision with the migration
  script's own schema-free changes — confirmed before either session
  touched `db.py`). `scripts/car_library/vendor/build_aac_library.py`
  (Phase 2B) — wired up its previously dead `-14.0` LUFS target the same
  way. Also: `scripts/car_library/` (the whole toolchain, vendored ORPHEUS
  lib included) had never actually been committed to git despite being
  confirmed execution-verified above — brought in for the first time
  alongside this change rather than leaving it half-tracked. Both halves
  dry-run by default, not yet run against the real vault. 395/395 passing,
  ruff/mypy clean on `musaeus/`+`tests/` (CI scope); the vendored
  toolchain carries pre-existing legacy-typing lint debt outside CI's
  lint target, untouched by this change.
- **Phase 3 built, 2026-08-18 (second Claude Code session):**
  `scripts/usb_transfer/transfer_to_usb.py` — standalone, not a pipeline
  Stage, same shape as Phase 2A/2B. Wipes + reformats a USB drive to
  ExFAT (Android target), then copies the chosen library (`--library
  alac` or `car`) with speed-monitored, checksum-verified transfer.
  Safety-gate design (typed device path + size, plus an independent
  denylist check) was confirmed with Grey *before* any code was written,
  not decided unilaterally — mirrors and extends §4.13's "no AI tool call
  can trigger a destructive path non-interactively" rule for DB resets,
  applied more strictly since a physical wipe has no DB-snapshot
  rollback. Denylist resolves the real backing disk of `/`, `/home`, and
  the vault via `findmnt`/`lsblk` at run time (not a hardcoded path list)
  and blocks the whole parent disk of any critical partition; re-checked
  again after the typed confirmation completes, since mount state can
  change mid-prompt. New `--extra-critical-mount` flag (repeatable) lets
  Grey pin additional drives into the denylist explicitly, since lsblk's
  removable flag is a heuristic, not a guarantee.
  **Self-caught bug:** the denylist-resolution function's own docstring
  claimed "fail closed" (refuse if a critical path's backing disk can't
  be determined) but the first implementation actually failed *open*
  (silently returned an incomplete set) — caught by its own test suite
  before shipping, fixed to raise `DenylistResolutionError` instead, all
  three denylist call sites in `main()` now refuse cleanly on that
  exception rather than proceeding with a partial answer.
  **Real finding from a read-only smoke test:** running the (safe,
  lsblk-only) `--list-devices` path against this actual machine showed
  `/dev/sdb` is currently `/mnt/NUC8TB_BACKUP`, a real 8TB backup drive —
  not a spare test device, and not the placeholder used throughout this
  script's own docstrings/examples. It's excluded from the removable-
  device picker only incidentally (lsblk reports it non-removable), which
  is what motivated the `--extra-critical-mount` flag above rather than
  relying on that heuristic alone.
  **Not yet tested against real hardware** — no test USB drive was
  available. `build_wipe_and_format_commands()` (pure command
  construction) and `copy_with_verification()` (plain file I/O, no
  hardware dependency) are both exercised for real; `execute_commands()`
  with `dry_run=False` (the actual `wipefs`/`parted`/`mkfs.exfat` calls)
  is only ever exercised via a monkeypatched `subprocess.run` recorder.
  Manually verify against a real, deliberately-expendable drive before
  trusting this with anything that matters. Speed-drop/cooldown
  thresholds (40% rolling-average drop, 15s cooldown) are first-pass
  defaults, not measured against real hardware.
  **Coverage closed, same day, on Grey's request:** `main()`'s own CLI
  orchestration (device selection, both denylist checks, confirm/execute
  sequencing, mount/copy/umount wiring) was the one real gap after the
  initial 23 tests — 62% file coverage, all of it in `main()` itself,
  every other function already fully exercised in isolation. 19 more
  tests closed it to 94% (remaining gaps: minor lsblk/findmnt
  exception-handling edges and the unreachable `if __name__ ==
  "__main__"` guard line). Also wired real subprocess coverage tracking
  for Phase 2A/2B's own subprocess-invoked tests (`build_alac_library.py`
  69%→78%, vendored `build_aac_library.py` newly measured at 69% —
  previously invisible to `pytest-cov`, which can't see inside a
  subprocess without help): opt-in via `MUSAEUS_COVERAGE_SUBPROCESS=1`,
  new `[tool.coverage.run]` in `pyproject.toml` (`parallel = true`, so
  each subprocess writes its own data file, combined via `coverage
  combine` after). Two real bugs surfaced and fixed while wiring this,
  neither a coverage-tooling nit: (1) `--rcfile`'s relative `source`
  path resolves against the *subprocess's* cwd, not repo root — silently
  traced nothing when a test ran with `cwd=VENDOR_DIR`, fixed by dropping
  `--rcfile` from the child invocation entirely (source-filtering only
  needed at report time); (2) `conftest.py` deliberately redirects `HOME`
  to a fake session directory all test-session long, specifically so
  `config.py`'s `_load_env()` can't leak real
  `~/.config/musaeus/credentials.env` into tests (correct, load-bearing,
  not touched) — but that same redirect broke `python3 -m coverage`'s own
  `~/.local`-based module resolution in any subprocess that inherits it;
  fixed by pointing `PYTHONPATH` at coverage's real install location
  explicitly, rather than restoring `HOME`. 42 tests total on Phase 3
  (441/441 project-wide), ruff/mypy clean. **Still not committed** —
  built and reviewed in this thread, handoff to the first session for
  commit (matching how Phase 2A/2B were committed) still pending as of
  this fold-in.
- **Audit hash-index false-positive bug — found and fixed 2026-08-18.**
  The 2026-08-18 overnight run's `AuditStage` flagged 10,415 problems
  (`finalized row's hash missing from persistent cross-batch index`) —
  the run itself completed cleanly (Enrich/MBEnrich both `OK`, process
  exited normally); this was `AuditStage` correctly refusing to report
  success, not a crash. Root cause: Check 3 (`musaeus/stages/audit.py`)
  required an exact `(audio_hash, file_path)` match against
  `hash_index.db`, but `finalized_hashes.file_path` is documented as an
  immutable snapshot "at time of finalize," never kept in sync with
  `archive.file_path` afterward — so any row finalized once and later
  quarantined by `DupeResolverStage` (which does update
  `archive.file_path`, per its own `UPDATE` at ~line 389) always failed
  the check even though its hash was genuinely present in the index. The
  actual production dedup lookup, `db.lookup_finalized_hash()` (used by
  `CrossDupeStage`), already keys on `audio_hash` alone — Check 3 now
  matches that. Not data loss: files and hashes both existed throughout,
  just under mismatched paths across two tables. Fixed (commit
  `54fd880`), regression test added simulating the exact
  finalize-then-quarantine sequence, verified against the real vault
  after landing: `AUDIT PASSED — all 11,800 finalized row(s) confirmed in
  the persistent cross-batch hash index`. 396/396 passing.
- **Pending duplicate-group staleness — found AND fixed, 2026-08-18.**
  Investigating the (separately known, non-data-loss) 12,242
  `DupeResolverStage` "file missing on disk" errors from the same
  overnight run: the `duplicates` table held 422 rows stuck
  `status='pending'` (17,247 `archive`, 16,512 `keep` by comparison).
  Checked all 370 distinct `file_path`s behind those 422 rows directly
  against disk — 0 of 370 existed at their recorded path. Same
  stale-`file_path`-after-move bug class as SOP §4.12 and the audit fix
  above, confirmed a third place it bites: `duplicates.file_path` itself
  (the code's own docstring already flags this as expected — "goes stale
  the moment a file is later [moved]"). Root cause: `_move_losers()`'s
  missing-file branch had no way to tell "genuinely lost" from "already
  moved by a different group/run" — it just errored and left the row
  `pending` forever, so `DupeResolverStage` re-selected and re-errored on
  the exact same rows every future run. Confirmed the actual mechanism
  via the `events` log: each stale row's missing path had a matching
  `DUPE_MOVED_FOR_REVIEW` event proving a *different* group already
  relocated the same physical file (a title re-flagged as a duplicate a
  second time after already being quarantined once). **Fix (`a9a31f5`):**
  check the events log before erroring; if a matching move is found,
  mark the row `archive` instead. **Verified live against the real
  vault:** 422 pending → 1 on the first real run (the one remaining row
  is a literal test-artifact filename, unrelated). Regression test
  simulates the exact cross-run staleness scenario. 420/420 passing at
  commit time.
- **`FinalizeStage` not yet writing to `ALAC_Archive` — confirmed,
  handled operationally rather than fixed in code, 2026-08-18.**
  Claude(chat) flagged that §5's roadmap bullet ("Phase 1 lands new work
  in `ALAC_Archive` going forward") reads as current behavior when it
  might only be the plan. Confirmed directly in code:
  `finalize.py`'s `_target_path()` uses `ctx.alac_library`, and
  `config.py` has no `alac_archive` property at all — every finalized
  file still lands in `ALAC-Library`, unchanged. Decided not to repoint
  Finalize now: real Phase-2A-cutover work (would also require reworking
  `AuditStage`'s Check 1/2, which currently hardcode "must be under
  ALAC-Library" — the same check fixed once already tonight), and
  unnecessary since Phase 2A is a standalone, manually-invoked script
  (same as Car-Library), not something the pipeline runs automatically.
  The actual fix is operational: re-run `musaeus_migrate_to_archive.py`
  before each Phase 2A pass to sweep in anything finalized since the
  last sweep. To stop that from depending on memory alone,
  `build_alac_library.py` now checks for it itself (`c8626e5`): counts
  finalized rows still under `ALAC-Library` before processing and prints
  a clear, non-blocking warning if any exist, using the same selection
  query as `migrate_to_archive.py`'s own candidate check so the two
  scripts agree on what "not yet migrated" means. Confirmed live
  (read-only dry run): flags exactly 1,385 unmigrated rows, matching the
  overnight run's own `CATALOGUED` count — nothing has been migrated
  into `ALAC_Archive` since the migration script was written.
- **Article handling is now defined in exactly one place, 2026-08-21.**
  `_ARTICLE_SUFFIX_RE`/`_ARTICLE_COMMA_RE` existed as byte-identical
  copies in both `normalize.py` and `enrich.py`. That duplication is the
  mechanism behind this convention regressing three separate times, and
  it bit again: the parenthetical-vs-comma bug fixed in `normalize.py`
  was still live in `enrich.py`'s reverse transform, turning
  `"Beatles, The (the)"` into `"The Beatles, The"` — a name Last.fm
  matches nothing against, so those artists silently never got enriched.
  `enrich.py` now imports both regexes from `normalize.py` (which owns
  the canonical storage form), and a test pins the shared-object
  identity so they cannot be quietly re-forked. **Rule: article logic
  lives in `normalize.py`. Anything else that needs it imports it.**
- **`(the)` was treated as an already-correct suffix, 2026-08-21.**
  `_move_article_to_suffix` OR'd the *wrong* form (`(the)`) and the
  *right* form (`, The`) into a single "already has suffix format,
  return as-is" check — so artists in the wrong form were classified as
  correct and passed through untouched. Normalize could never fix them,
  because Normalize believed there was nothing to fix. Live impact:
  1,262 rows / 203 distinct artists. Fixed, backfilled (156 CATALOGUED
  rows, 54 files relocated, 142 empty dirs pruned, AUDIT PASSED after).
  The 1,106 `DUPE_REVIEW` rows were deliberately left — Normalize only
  processes `CATALOGUED`, and those sit in a holding area.
- **`albumartist` was never written by any stage, 2026-08-21.**
  `TaggerStage` wrote artist/album/title/genre/year/track and nothing
  else, and there is no `archive.albumartist` column, so the field kept
  whatever the source file arrived with — permanently. 2,035 of 5,894
  article-artist files (34.5%) had artist and albumartist disagreeing.
  Now **repaired, not mirrored**: rewritten only when the existing value
  is provably the same artist in a non-canonical spelling (normalizing
  it lands on the DB artist). A genuinely different albumartist is left
  alone, because it legitimately differs on compilations ("Various
  Artists") and split credits ("Johnny Cash, The Tennessee Two").
  Backfilled: 572 files rewritten, 0 repairable mismatches remaining.
- **The event log is an audit trail, NOT an event-sourcing store —
  confirmed 2026-08-21, and this distinction is load-bearing.**
  `rebuild.py` was built on the premise that "the event log is the
  immutable source of truth" and replayed it to reconstruct `archive`.
  That premise is false: the log is human-readable and lossy by design.
  `HASH_COMPUTED` stores `"0faef0355d05cb91…"` — 16 characters and a
  literal ellipsis, where a real sha256 is 64 — and
  `METADATA_EXTRACTED` records only artist/title/bitrate, never
  album/genre/year/track/duration/codec. Separately, all ten event names
  it dispatched on had **zero overlap** with the 34 that actually exist.
  It ran `DELETE FROM archive` *first*, so against the real vault it
  would have wiped ~27,000 rows and left `PENDING` stubs while reporting
  success. Now disabled (fails closed, exit 2). **Do not build recovery
  tooling on the events table.** The real recovery paths are the
  pre-reset DB snapshot in `ALAC-Library/_history/` (which actually
  recovered the vault on 2026-08-20) and a rebuild from disk + embedded
  file tags.
- **Tests that assert the implementation instead of reality —
  the pattern to watch for.** `rebuild.py` had 13 passing tests and
  could never have run: they fed it the event names the code expected
  rather than the ones any stage emits, so a module that was entirely
  dead code sitting in front of a `DELETE` looked well covered. The same
  shape recurred elsewhere: `TranscodeStage`'s extension filter excluded
  `.m4a`, so it had zero eligible rows in a real library and an
  album-art-stripping bug behind it went unnoticed; `ForgeStage`'s
  tag-read shortcut was never exercised against a genuinely forged file.
  **A stage is not done until it has run against real data once.** Green
  tests are not evidence that a stage works — only that it behaves as
  its author imagined.
- **Bit-rot detection — redesigned mid-session, 2026-08-19/20 (commit
  `d612728`, superseding `78b39a7` same night).** First design compared
  a re-hash of `CATALOGUED` files against `archive.full_hash`
  (Sentinel's own hash, computed at intake). Live-vault testing found
  that stale for nearly every finalized file: `Canonicalize`/`Forge`/
  `Tagger` all legitimately rewrite file bytes after Sentinel computes
  `full_hash`, so it was never actually valid for post-Finalize
  corruption checks — fine for what it was built for (Sentinel's own
  retag-vs-audio-change detection), never meant to survive the rest of
  the pipeline. Corrected per Grey's own framing ("trust the archive,
  establish a fresh baseline"): `BitRotStage` now scans `ALAC_Archive`
  directly (not `archive.file_path`/`archive.id` at all — matching
  `ALAC_Archive`'s own deliberately-not-DB-row-tracked design) and
  keeps its own baseline table, `archive_tier_hashes`, keyed by path.
  `--rebaseline` establishes/refreshes the baseline explicitly, never
  automatically; the default mode verifies against it, reporting
  corrupt/new/missing separately. Ran `--rebaseline` against the real
  vault: 1,385 files baselined, matching `ALAC_Archive`'s exact file
  count.
- **Forge tag-read shortcut — added 2026-08-20 (commit `8bc1185`),
  mirroring BPM's existing pattern.** Grey asked whether MUSAEUS stores
  enough on a file's own tags to avoid redundant reprocessing after a DB
  reset — checked directly and found the answer was yes for BPM (tag-
  read-first shortcut already existed) but no for Forge: it only gated
  on the DB's `rg_tagged_at` column, so a DB wipe followed by re-
  ingesting already-forged files meant burning real ffmpeg time re-
  measuring EBU R128 loudness already sitting in the file's own
  ReplayGain/R128 tags. `read_existing_rg_tags()` now reads
  `com.apple.iTunes.R128_TRACK_GAIN` (M4A) or `REPLAYGAIN_TRACK_GAIN/
  PEAK` (FLAC/MP3) and recovers `lufs` directly — a physical property of
  the audio, not of any particular reference level, so the shortcut
  stays correct even if `--target-lufs` differs from whatever reference
  produced the original tag. `--retag` skips it, same escape hatch as
  BPM's own `--retag`.
- **BPM multichannel skip — added 2026-08-20 (commit `32bfeff`), found
  live during the first real-scale BPM run (below).** Essentia's
  `MonoLoader` can't decode anything but mono/stereo. Grey's call: no
  interest in multichannel audio outside of stereo, so a multichannel
  file is now a permanent skip, not a retry-forever error — checked
  proactively via `archive.channels` (already populated by Scholar)
  before ever calling Essentia, with the exception-message match kept
  as a fallback. `bpm_analyzed_at` gets set (bpm/key/energy/danceability
  left `NULL` — a legitimate terminal state, not a failed analysis to
  retry) and one row is appended to `TuneMyMusic.csv` (reusing
  `canonicalize.py`'s existing helper/convention) for manual review/
  replacement with a stereo source, guarded against duplicate rows
  across repeated runs.
- **First full-scale real-vault run of the reordered pipeline —
  2026-08-19 night into 2026-08-20 (this session).** Full USB2
  `Curated.RAW.Files` backlog (11,583 files, 439GB) copied into `INBOX`
  and run through `musaeus run` for the first time since BPM/
  VariousArtistsFix were wired in. Act 1/2/3 completed cleanly (15,653
  finalized). BPM was ~66% through (3,459/5,218) when the process was
  killed by the Linux OOM killer around 01:29 — confirmed via
  `journalctl` ("A process of this unit has been killed by the OOM
  killer"), not a bug in the pipeline itself; swap was fully exhausted
  at that moment, RAM was fine before and after. Restarted (screensaver-
  style idle-aware wrapper, `scripts/idle_run.sh` — new sibling to the
  pre-existing `idle_forge.sh`, same reviewed pattern, built this
  session since `idle_forge.sh` is deliberately scoped to standalone
  `musaeus forge` only) and **resumability held end-to-end through a
  real kill+restart, not just in tests**: every file already past BPM
  (or any earlier stage) picked up exactly where it left off, none
  redone. Confirmed the multichannel-skip fix (above) live against the
  real vault this same run, not just via unit tests: all 5 known
  multichannel files correctly got `bpm_analyzed_at` set with `bpm`
  left `NULL`, 5 `BPM_SKIPPED_MULTICHANNEL` events logged, and 5 rows
  landed in `TuneMyMusic.csv` with correct codec/channel/duration data.
  Also surfaced, live, two edge cases worth knowing about rather than
  fixing: a small number of genuinely corrupted/truncated files
  (`moov atom not found` on ffprobe) and files with extreme durations —
  one ~12-hour ambient/sleep-sound track cost two sequential 600s ffmpeg
  timeouts (~20 minutes) in `ForgeStage` before being correctly marked
  failed and skipped, since loudness measurement's timeout caps at 600s
  regardless of source duration. As of this session's end, the run was
  still in progress (Forge stage, ~95% through) — not yet confirmed
  complete through Tagger/Audit/Enrich/MBEnrich.
- **Live-DB reconciliation after a hard reset — 2026-08-19/20, this
  session.** Grey ran a hard reset (console option) to test the newly-
  wired stages against a small batch first, which correctly wiped the
  DB's memory of the entire pre-existing real library (per the reset's
  own on-screen warning) while leaving the files themselves untouched on
  disk. This surfaced `AuditStage` failing against ~1,385 "real files
  with no matching DB row" once a larger run followed — not data loss,
  just the DB's bookkeeping falling behind disk state, exactly the kind
  of gap §4.1's disk-vs-DB verification discipline exists to catch.
  `musaeus/rebuild.py` (event-log replay) was investigated as the fix
  and found to be **stale and non-functional for this**: none of its
  handled event-type names (`FILE_REGISTERED`, `FILE_HASHED`, etc.)
  match what the current stage code actually logs (`INGEST`,
  `HASH_COMPUTED`, `FINALIZE_MOVE`, etc.) — running it would have
  silently produced a near-empty, wrong archive table. Not fixed this
  session (out of scope for the immediate need); flagged as a real,
  separate latent bug. Actual fix used instead: the console's own
  pre-reset DB snapshot (`ALAC-Library/_history/musaeus_pre_reset_*.db`)
  already had every pre-existing row, correctly populated by real stage
  execution, not reconstruction — copied forward as the new live DB,
  then the post-reset DB's own already-correct rows/events (for the
  small test batch already run) were merged on top via a direct
  `ATTACH DATABASE` copy, letting SQLite autoincrement assign fresh,
  non-colliding IDs. Verified against a scratch copy first (`AuditStage`
  run read-only against it) before ever touching the live path.
  Confirmed via the real `musaeus audit` CLI command afterward: all
  11,815 finalized rows verified present, all confirmed in the
  persistent cross-batch hash index.

## 6. Salvaged from ORPHEUS

- `orpheus_noise_masker.py` — confirmed working via actual execution
  (measured ~27dB high-frequency masking), vendored into MUSAEUS's own
  repo (`scripts/car_library/vendor/`) rather than read from the
  live/backup ORPHEUS install at runtime.
- `orpheus_noise_generator.py` — pink/brown/white noise AAC track
  generation (30/60-minute lengths, two-pass loudnorm to -16 LUFS/256k
  AAC), the natural sibling to `noise_masker.py` above. **Built and
  vendored, 2026-08-19 (second Claude Code session), closing a real gap
  a full 222-script ORPHEUS audit surfaced that night** (Grey split four
  such gaps across the two sessions by ownership; this one was this
  session's). Confirmed working via actual execution, not just import:
  `generate_track()` called directly with a short duration override,
  real ffmpeg pipeline, output verified via ffprobe (real AAC stream,
  correct duration) and re-measured loudness (landed at -16.1 LUFS,
  target -16.0). Only real ORPHEUS-internal dependency was
  `lib.orpheus_paths.RUNS_ROOT`, used solely to compute the output dir —
  removed entirely; now reads `ORPHEUS_NOISE_DIR` from the environment,
  same override pattern `noise_masker.py` already uses. New thin
  MUSAEUS-side wrapper, `scripts/car_library/generate_noise.py` (mirrors
  `build_car_library.py`'s own shape), sets that env var to
  `<vault_root>/RUNS/Noise` — the same location `build_car_library.py`'s
  masking step and `curator.py`'s `_find_noise_files()` already expect
  noise tracks to live, so nothing downstream needed to change. Standalone,
  occasional-use tool (regenerating noise tracks is a rare prep step, not
  part of a normal export run) — not wired into `DEFAULT_PIPELINE` or
  `musaeus run`, same precedent as everything else in this section. 4 new
  tests, real execution, all passing. This closes out the "not-yet-built"
  status the 2026-08-19 correction above (superseded, kept only as
  history) had just caught.
- `orpheus_audio_analyzer.py` — BPM/key/energy/danceability via Essentia
  (`musaeus/stages/bpm.py`, `BPMStage`), confirmed working via real
  end-to-end testing (plausible BPM values, DB-vs-file-tag cross-check
  matched), including a full real-vault run at scale (§5). Built
  standalone first, **wired into `DEFAULT_PIPELINE` 2026-08-19/20** —
  see §5's pipeline-reorder bullet. Design differences from the
  original: DB-row-driven (not a directory walk), resumability via
  `bpm_analyzed_at` rather than a separate mtime-tracking table,
  sequential rather than the original's `ThreadPoolExecutor` (sidesteps
  the OOM risk multiple concurrent Essentia workers create), tag-read-
  first shortcut kept (skip Essentia entirely if a prior BPM tag already
  exists on the file, unless `--retag`), multichannel audio permanently
  skipped rather than retried forever (§5).
- `orpheus_junk_quarantine.py` — tribute-band/karaoke/meditation-content
  detection (`musaeus/stages/tribute_quarantine.py`,
  `TributeQuarantineStage`), ported 2026-08-19. Standalone, not wired
  into `DEFAULT_PIPELINE`; added to the interactive console menu with a
  `[standalone, not part of the automatic pipeline]` notation so it's
  discoverable without being auto-run. Detection patterns ported
  verbatim including inline reasoning comments. Matches move to
  `ALAC-Library/TRIBUTE_REMOVED_FOR_REVIEW/`, never delete — same
  moved-not-deleted convention as `DUPES_MOVED_FOR_REVIEW`, including a
  CSV manifest + auto-generated bash restore script. `AuditStage`'s
  excluded-subdirs list updated so the new folder doesn't get flagged
  as orphaned content. Confirmed against real vault data (read-only):
  27 matches out of 1,385 rows checked.
- `fix_various_artists.py` — real-artist resolution for "Various
  Artists"-tagged rows (`musaeus/stages/various_artists_fix.py`,
  `VariousArtistsFixStage`), ported 2026-08-19. Strategies ported:
  bracket extraction, filename-segment splitting, MusicBrainz recording
  lookup (optional). The original's "track artist tag" and "album
  artist tag" fallback strategies were confirmed dead code / not
  practically usable in MUSAEUS's schema and dropped, not ported.
  A resolved row moves to the corrected `Artist/Album` location (same
  convention `FinalizeStage` itself uses) and both `archive.artist` and
  `archive.file_path` update together. Built standalone first, **wired
  into `DEFAULT_PIPELINE` 2026-08-19/20** — see §5.
- `orpheus_integrity_check.py` — silent bit-rot detection
  (`musaeus/stages/bitrot.py`, `BitRotStage`), ported 2026-08-19,
  **redesigned same night** once the first design's approach (re-hash
  against `archive.full_hash`) was found stale by live testing — see
  §5's dedicated bullet for the full story. Standalone, not wired into
  `DEFAULT_PIPELINE`. The original's `--deep` frame-level scan was
  dropped — `CorruptStage`'s existing ffprobe decode-test already covers
  that ground more reliably than hand-rolled frame-sync byte scanning.
- Explicitly **not** salvaged: ORPHEUS's `orpheus_car_playlist_builder.py`
  and MasterLaw-based playlist system — MUSAEUS's own `playlist.py`
  already covers this, confirmed working (98%+ genre coverage against
  the live DB), and porting a second system would risk the same
  duplicate-implementation pattern that caused the article-suffix bugs.

## 7. Open / deferred initiatives

Tracked in `MUSAEUS_OPEN_ITEMS.md` (current open state — disposable,
rebuilt each session rather than accumulated) and `MUSAEUS_TODO.md`
(execution order), not here. This section is intentionally kept empty
of status content: §2 and §5 above hold confirmed standing facts about
what exists; anything not yet confirmed, not yet built, or still in
question lives in those two files instead. If resolving an open item
produces a genuinely durable standing rule or convention, it gets
folded into §4 or §5 directly, not left here.

## 8. Explicit boundaries

- Don't route real files through ORPHEUS while it's mid-repair and
  unverified by this project's own standards.
- Don't run two live writers against `musaeus.db` concurrently without
  first confirming lock/contention behavior — WAL mode and
  `busy_timeout` reduce but don't eliminate the risk, and concurrent
  writes make the DB-vs-disk verification discipline (§4.1) much harder
  to reason about. `musaeus_overnight.sh` now enforces this directly
  (§4.11) rather than relying on the honor system.
- Don't build a direct AI-to-AI bridge between this chat and Claude Code
  — the human-in-the-loop relay is intentional, not a workaround for a
  missing feature (see handoff for the fuller reasoning).
- Don't let automated code-review or self-heal tooling auto-commit
  anything. Report-only, human-approved-by-hand, until an explicit
  review gate exists and is confirmed working — see §4.10 for the
  incident that makes this non-negotiable rather than precautionary.
- Don't treat a `finalized_at` (or similarly "done") DB flag as proof a
  file actually moved or an index actually updated — the hash-index gap
  (tracked in `MUSAEUS_OPEN_ITEMS.md`) is a confirmed case of the DB
  saying "done" ahead of reality.
  Cross-check disk state directly per §4.1.

## 9. Update log

- **2026-09-18/19 sessions (Claude Code), the library wiped and rebuilt from
  source, and one rule that came out of it:**

  **The rebuild.** `musaeus.db` was reset and `Libraries/` (929 GB, 9,064
  masters) deleted, then rebuilt from `USB1/2.- Curated.RAW.Files`. Grey's
  call. It was made survivable by measuring four things first, in order:
  coverage (a PCM-identity check found **1,586 catalogued tracks — 18% — with
  no counterpart in the raw folder**, which a wipe would have destroyed);
  soundness (every one of 12,286 raw files decoded, not probed — 12 damaged,
  11 never ingested); a verified 929 GB backup on the NUC; and the rulings
  exported to CSV keyed on `audio_hash`.

  **The LEDGER is the thing that made the reset affordable.** 2,521 denied
  hashes survived at `_db_backups/hash_index.db`, outside `Libraries/`. On the
  first rebuild batch it refused 17 of 50 files unaided — deletions declining
  to come back without a human re-reviewing one of them.

  **The archive files under `Genre/Artist/Album`** (Grey, 2026-09-18). Safe
  because MasterLaw rules genre per ARTIST: measured before the change, 0
  artists spanned more than one genre and 0 albums would be split.

  **A new standing rule, learned the expensive way:**

  > Anything MUSAEUS can rebuild lives in `Libraries/`.
  > Anything it cannot lives outside it.

  `TuneMyMusic.csv` — 311 rows of Grey's wanted list, pure human judgement —
  was stored in `Libraries/ALAC-Archival/` and the wipe took it. Recovered
  from the NUC backup by luck rather than design. The same defect still
  applies to `DUPES_MOVED_FOR_REVIEW` and `TRIBUTE_REMOVED_FOR_REVIEW`, which
  hold 367 audio files awaiting Grey's decision *inside* the wipeable tree;
  agreed 2026-09-19 to move both to `VAULT/REVIEW/`.

  **The source folder was not the origin anyone believed it was.** Beyond the
  18% coverage gap: three article conventions in one folder (1,033 files as
  `Artist (the)`, which MusicBrainz has never heard of, so a tenth of the
  Picard work was failing before it began); 438 filename collisions that were
  *different recordings* rather than duplicates, which a naive rename would
  have overwritten; 171 karaoke and tribute products; and one 0-byte file
  reporting a valid duration. All fixed at source.


- **2026-09-14 → 2026-09-17 sessions (Claude Code), the three editions
  finished and four standing rules changed:**

  **Three fields, three jobs (Grey's ruling, enforced 2026-09-16).** An
  artist has a *name*, a *sort key* and a *folder*, and they are no longer
  the same string. The `artist` tag is the **natural form** (`The Beatles`),
  because that is the only form any external service has heard of; the `soar`
  tag is the **sort form** (`Beatles, The`); the **folder** is the sort form,
  so a car head unit browsing directories still files it under B. Before
  this, `archive.artist` held the sort form and 1,071 proposal rows — 99.9%
  of every article artist in the library — had been asked about under a
  string no service recognises and come back empty. MusicBrainz hits on
  those artists went 0 → 491. `NormalizeStage` was rewriting the tag back on
  every run and is now the thing that enforces the rule instead.

  **Album capitalisation (Grey, 2026-09-16): minor words stay lower-case.**
  "at", not "At". `musaeus/title_case.py` holds the rule and its 26-word
  `MINOR_WORDS` set; "so" is deliberately absent. 80 case-duplicate album
  folders were merged across the tiers as a consequence — where two folders
  differed only in case, the better copy won on quality, then on length.

  **Deleted means deleted (Grey, standing, reaffirmed 2026-09-16).** The
  review tool `scripts/delete_reviewed_tracks.py` removes a track from all
  three tiers *and* denies its hash in the LEDGER, so a re-ingest cannot
  bring it back. It ran in seven reviewed batches over two days, every row
  reviewed by Grey from a CSV first. Two operating rules came out of its first run: a
  car file may be shared by more than one catalogued row (77 are), so the
  tool refuses to delete a file a surviving row still points at; and a
  title-only match is not identity — a loose match would have deleted Taylor
  Swift's "Blank Space" because a DJ mix of "Shake It Off" existed. The catalogue is
  **438 rows smaller** than on 2026-09-14; that is the net change between two
  totals, and it is stated that way on purpose rather than presented as a
  count of deletions.

  **A ruling belongs in a file, not in a session.** `artist_canon.tsv`
  gained a guard that refuses to write an entry which chains
  (`Jeff Lynne's ELO → ELO → Electric Light Orchestra` resolves to nothing),
  after one was written by hand. Five acts filed under two names each are now
  one: Electric Light Orchestra, John Mellencamp, Bob Seger, Paul McCartney
  (Wings folded in), and The Beat.

  **What is now built.** All three editions exist and `doctor` checks the
  rule that none is built from another: `ALAC_Library` (11,113 files,
  −18 LUFS), `CAR_Library` (10,654, AAC 256k, −14 LUFS, noise-masked with a
  true-peak ceiling — 19% would otherwise have clipped), and
  `iPHONE_Library` (5,693, AAC 256k). The bake backlog that this document's
  companion called "the number to watch" went 2,821 → 4.

  **Durability, twice.** AcoustID fingerprints now live in the LEDGER
  (`_db_backups/hash_index.db`), keyed on `audio_hash`, so a database
  rebuild no longer throws away every fingerprint — the failure that had
  already happened once. The same treatment is owed to `archive_tier_hashes`,
  which holds **0** bit-rot baselines today against 1,385 on 2026-09-08,
  lost in the 2026-09-10 reset.

  **A transfer the operator can actually perform.** `scripts/iphone_transfer.py`
  prints the `ifuse` commands and never runs them. iOS sandboxing means a
  file placed in VLC's container can never be seen by the Music app, so a
  script that "just does it" would silently produce a library the phone
  cannot play.

- **2026-09-03/04 sessions (Claude Code), verification honesty and path
  limits:** Added SOPs §4.27 (verify the claim, not a constant), §4.28 (255
  BYTES per path component; a deterministic failure needs isolation, not
  retry) and §4.29 (a wanted-list entry must clear `already_owned`).

  The generated per-run document is now **`ForClaudeHandoff_<run_id>.md`**,
  renamed from `handoff_<run_id>.md` to stop it being confused with Grey's
  hand-written `MUSAEUS_HANDOFF_<date>.md`. The H1 inside it carries the same
  name, because the file name does not survive a copy-paste and that document
  exists to be pasted into a session with no file access. Its error lists are
  capped at twenty entries with `... and N more`: a single cleared directory
  produced 3,133 near-identical `Missing:` lines, which would have made a
  ~400 KB file that cannot be pasted anywhere. The same unbounded loop existed
  in `cli.py`'s console printer; both now share one helper beside
  `StageResult`.

  Curation rule refined (Grey, 2026-09-03): the test is **whether the RELEASE
  is the artist's own work, or a tribute/karaoke product** — not whether the
  recording is a cover. Springsteen's own *Blinded by the Light* never
  charted and is wanted; Manfred Mann's cover went to #1 and is wanted; Chuck
  Billy's track on *Metallic Assault: A Tribute to Metallica* is neither and
  was deleted. Jazz and blues standards are exempt: take the best copy. **This
  is explicitly not automatable** — a cover-detection rule would destroy all
  three of those examples. Album- and title-level evidence stays automated;
  the rest is a per-record judgement.

  **Resolved the same day, and it became §4.30.** A file Canonicalize
  detected as truncated and refused to convert was present in the new library
  anyway (Carlos Santana, "Bella" — source and library file share PCM hash
  `99e88ae6e1462dc5`). The refusal was not at fault and neither was Finalize:
  the audio was in INBOX **twice**, and the second copy was already ALAC, so
  it was classified PASSTHROUGH and no check ran on it. Both initial
  hypotheses — that the recorded duration had been corrected mid-run, and
  that something had set `canonicalized_at` — were disproved by the row
  itself, which still carries the false 268.226667 s and an empty
  `canonicalized_at`. The escaped copy has been deleted (the 268 s master in
  `ALAC_Archive/2026-08-27A` was verified present first) and `doctor` gained
  a check for the class. See `ForClaudeHandoff_santana_2026-09-04.md`.

- **2026-08-24 session (Claude Code), the year field and the MusicBrainz
  path:** Added SOPs §4.21 (a populated field is not a right one), §4.22
  (a high match score is not identity) and §4.23 (a decision is not
  applied until every file encoding it agrees).

  Finished the 2026-08-23 pipeline run: BPM completed all 1,357
  remaining files (10,667 analysed, 0 outstanding), then Forge, Tagger,
  Audit and Enrich. `doctor` reports one warning — 85 hash-ledger
  entries naming purged files — and `audit` passed all three checks
  against 10,672 finalized rows. Compared against
  `BASELINE_20260823T102456`: 173 files deleted, 15 moved to
  DUPE_REVIEW, **0 new**, 10,667 present in both, 2,950 genre changes
  (all draining `Pop, Rock`) and 2,939 BPM values gained. Note that the
  handoff's own purge figures disagree with each other and with this —
  154 in its summary, 81 in its breakdown, 173 measured.

  **`MBEnrichStage` was stopped mid-stage and its output partly
  reverted.** It had written 27 wrong artist MBIDs (§4.22); those rows
  were cleared back to unenriched so the corrected code will revisit
  them, with an `MB_MATCH_REVERTED` event recording each. 212 rows whose
  names differ only by punctuation or case (`R.e.m` → `R.E.M.`) were
  verified correct and kept. **This run emitted no `RUN_END` and
  should not be described as complete** (§4.20) — 23 of its 24 stages
  finished.

  Established that `year` is the edition year, not the recording year,
  and built `OriginalYearStage` (`musaeus original-year`) to recover the
  latter into a new `original_year` column. Four guards, each able to
  reject: search score, artist-credit agreement after article folding,
  track length within 8 s, and a refusal to accept an "original" later
  than the pressing on disk. Deliberately outside `DEFAULT_PIPELINE` —
  it is one rate-limited lookup per track — and it carries a `--genre`
  filter so a pending decision need not wait for a whole-library pass.

  Completed the `Pop, Rock` retirement the previous session recorded as
  finished (§4.23), and applied the owner's rulings on ten further canon
  map targets. Vocabulary now **49 genres** with `Baroque` admitted; **0
  map rules target a value outside it**, from 15.

  Widened `VariousArtistsFixStage` to non-artist credits generally, not
  just Various Artists: six *Pulp Fiction* tracks stored under the
  artist "Soundtrack" now carry their real performers, with genre taken
  from each artist's **own existing library rows** rather than from
  MasterLaw (§4.19's direction). Two defects the earlier Various sweep
  had left behind were fixed with it — the recovered artist was never
  removed from the title, producing `Eric Carmen - Eric Carmen - All By
  Myself.m4a`, and the natural form was written where the library stores
  the suffix form, which split The Revels and The Tornadoes in two.

  Bounded `ForgeStage` by duration, closing the gap §4.20 left open on
  the 23rd. Built a report-only truncated-artist discovery
  (`scripts/report_truncated_artists.py`, read-only by construction):
  141 raw candidates narrowed to 7, of which MusicBrainz confirms
  `and the Mysterians` is **? and the Mysterians**.

  USB2 verified against the NUC8TB backup — a full itemised comparison
  at 0 differences across 15,095 entries plus a 61-file checksum sample
  — and the 439 GB of redundant raw files cleared.

  **Split `Pop` into `Classic Pop` / `Modern Pop`** at the owner's
  direction — boundary 2000, artists assigned whole by majority of
  tracks, decided from `original_year` and never from `year`. The
  corrected data inverted the result: on raw `year` the same script
  produced 183 Modern / 26 Classic, on recovered recording years it
  produced **65 Modern / 102 Classic** (257 and 176 tracks). 42 artists
  with no recoverable year — nearly all multi-artist collaboration
  credits — keep `Pop`. Four exact ties were broken toward the artist's
  earliest recording, since a tie has no majority for the owner's rule
  to reach; two of those "ties" turned out to be the same song held
  twice under different titles (Gene Pitney, Toni Basil), which dedup
  should now catch. Verified afterwards: 0 multi-genre artists, 0 law
  disagreements, 0 artists without a rule.

  Two library-wide gaps surfaced that no document had recorded:
  **`track` is NULL on all 10,667 files** and 4,926 (46%) have no
  album. `validation_issues` is an unbounded log at 343,938 rows, 75% of
  them naming paths that no longer exist.

  **Built a deny-list** (`DenyListStage`, §4.24), the mechanism that was
  actually missing: removing a file never stopped it returning, because a
  ledger hit is only acted on when a live file backs it. Sits after
  Sentinel, matches on `audio_hash`, quarantines rather than deletes, and
  never touches already-catalogued music. Seeded with 84 removed
  recordings and proven end-to-end against the live vault — a genuinely
  deleted file was returned to the INBOX and refused, with the library
  unchanged at 10,665.

  **Merged the P0 consumer-readiness branch** (18 commits): the
  execution-authority gate (opt-in, off by default), a CLI inventory
  drift guard, the evidence bundle, and a `network_policy` scope fix —
  the gateway is module-level mutable state, so a caller granting ALLOWED
  and not restoring it left everything afterwards in the process
  permissive. Clean merge, only `db.py` overlapping.

  **Two stages joined Act 1.** `GenreValidateStage` had never been in
  `DEFAULT_PIPELINE` at all: it applies MasterLaw, fills empty genres and
  enforces one-genre-per-artist, and only ever ran when invoked by hand,
  which is why a file's genre came from its own tags via Scholar and
  nothing checked it against the law. `ClassicalComposerStage` follows
  it, filing Classical under the composer — resolved from a thematic
  catalogue number first (RV 532 is Vivaldi whoever is playing; 85 of 104
  resolved that way), then an exact match against `Composer_Canon.tsv`.

  **Artist identity work.** 145 collaboration credits added to
  `artist_canon.tsv` and applied (149 tracks); classical refiled under
  composer (104 tracks — Bach 59, Vivaldi 51, Handel 23, Mozart 17); 21
  crossover covers moved from Classical to Easy Listening; the
  case-split `Olivia Newton-john` / `Olivia Newton-John` merged. Each
  broke one-genre-per-artist or law agreement on the way through, and
  each was restored before moving on — the checks were run *after*
  applying rather than before, which is the process error worth
  remembering.

  **`console.py` never granted network authority** — zero references to
  `network_policy`, so every network stage launched from its menu was
  refused while the run reported success. Now granted through the scoped
  helper, preview excluded, with the call sites pinned by test.

  **The hand-curated canon existed in exactly one place.** MasterLaw,
  `artist_canon.tsv`, the vocabulary and both new canon files live only
  in `MUSAEUS_VAULT/MetaData` — the music is mirrored twice, the
  decisions were not backed up at all. Now copied to
  `/mnt/NUC8TB_BACKUP/MUSAEUS_METADATA/` (252 KB, `latest/` plus a
  timestamped snapshot). **This needs to join whatever backup routine
  runs; it was done once, by hand.**

  Tests 768 → **1,337** (1,331 of them after the merge); lint clean across `musaeus/`, `tests/` and
  `scripts/`, with vendored ORPHEUS code excluded from ruff.

- **2026-08-23 session (Claude Code), genre canon and long-form audio:**
  Added SOPs §4.18 (a verification must be falsifiable), §4.19 (sync runs
  outward from the source of decisions, and you measure first) and §4.20
  (bound the work; a C extension cannot be interrupted by a timeout).

  Retired the **`Pop, Rock` catch-all** — 3,004 tracks across 657
  artists, 28% of the library and its largest "genre", though it was
  never a genre, only the bucket things landed in undecided. Drained to
  0 over four rounds of owner decisions applied to both the library and
  MasterLaw, then removed from the vocabulary along with the 192 law
  rows that would otherwise have refiled future ingests into it.

  Fixed a disagreement between the law and the library about **what an
  artist is called**. `GenreLaw._key()` never folded the article
  convention, so MasterLaw's pre-normalisation spellings
  (`5th Dimension (the)`, `The Byrds`) produced keys the library's
  `", The"` form could never match: **246 rules were dormant**, and a
  dormant rule is indistinguishable from an absent one. Folding the key
  then exposed **41 pairs where one artist sat in the law twice** with
  two spellings and two different genres, whichever sorted last silently
  winning. Law and library now agree on every artist — 0 disagreements,
  0 library artists without a rule.

  Completed the effect-verification work started on the 22nd
  (Canonicalize, Normalize, Sanitize, GenreValidate), and in doing so
  found three checks that could never have fired — see §4.18.

  Removed 81 files at the owner's direction: guided meditation and
  hypnosis content (35), knock-off tribute/karaoke/compilation acts (26)
  and a taste call (20). Established that **`TuneMyMusic.csv` is not an
  import format** — it carries no artist or title, and for a knock-off
  the only artist in the row is the knock-off itself, so uploading it
  would have re-acquired exactly what was removed. A usable derivative
  is now generated with originals restored.

  Vocabulary 51 → **48 genres**; MasterLaw 3,669 → **3,378 rows**; tests
  747 → **768**.
- **2026-08-11 session (Claude Code):** Added the overnight concurrency
  guard (§4.11) to `musaeus_overnight.sh`. Redesigned
  `CanonicalizeStage`/`FinalizeStage` around the `STAGING` write-then-
  verify flow (§5) and the collision-safe DB-write pattern (§4.12).
  Investigated and confirmed only the real `MUSAEUS_VAULT` (not
  `MUSAEUS_TEST_VAULT`) was hard-reset and rebuilt that day, via the two
  guarded reset paths (§4.13) — a deliberate human action, not an
  accidental wipe or automation bug. Confirmed `P0-01`/`P0-02` of the
  consumer-readiness safety spec done (§7).
- **2026-08-12 session:** Post-audit review surfaced a suspected
  canon-before-dedupe ordering regression, possibly introduced during the
  Act 1/2/3 restructuring — flagged as needing direct verification, not
  yet resolved (§2, §7).
- **2026-08-15 session:** Found the Finalize/hash-index atomicity gap and
  the empty `_history/hash_index.db` (§7), the missing pre-wipe snapshot
  (§7), and re-flagged the pipeline stage order as unconfirmed rather
  than settled (§2, §7). Refined the Car-Library independence
  requirements (§2, §7) and completed the PR #5 triage via direct
  execution rather than code-reading alone (§7).
- **2026-08-17:** This document folded in the above, primarily from a
  pasted Claude Code session transcript (2026-08-11) cross-checked
  against the 2026-08-12 and 2026-08-15 handoffs already in project
  knowledge.
- **2026-08-14 session (separate chat thread):** Discovered and
  recovered a real data-loss incident — 14,467 phantom `CATALOGUED` rows
  pointing at a vanished `ALAC-Library/2026-08-12` directory; 10,644
  recovered via cross-drive hash matching and relinked, 4,608 confirmed
  genuinely gone and marked `GHOST` (§7). Root cause left permanently
  open after direct investigation ruled out the leading theories.
  Independently found and fixed the DupeResolver stale-snapshot bug
  (§7, commit `3337537`). Found a near-miss with `GhostStage`'s
  unconditional full-table sweep during the recovery, now codified as
  §4.14. Fixed two real bugs in `transcode.py` (extension-based instead
  of codec-based lossless detection, meaning the entire real ALAC
  library was invisible to it; and an unconditional `-vn` silently
  stripping embedded album art on any future real transcode). Found
  `albumart.py` already existed and worked but was never wired into
  `DEFAULT_PIPELINE` (same category of gap as `GhostStage`) — wired in.
  Applied the JMS583 hardware fix (§7).
- **2026-08-17 session (same thread as 2026-08-14, continued):**
  Implemented the guest-credit-stripping convention for the first time
  (§5) — confirmed it had never actually been coded before this session,
  despite earlier documentation implying otherwise. Found and fixed two
  additional bugs in the same area: the `the`-prefix grouping gap in
  `_normalize_key()`, and the comma-heuristic collision bug duplicated
  identically in `artist_consolidate.py` and `curator.py` (commit
  `c559fa6`). Confirmed old `health.py`'s functionality is now fully
  covered by `CorruptStage`/`PreflightStage` except permission-fixing,
  which was built standalone as `PermissionsStage` but not yet tested
  against real dirty data. Found the tribute/karaoke non-original-
  recording problem is broader than first scoped (§7). This document
  cross-checked against the 2026-08-11/12/15 pass done earlier the same
  day — one real discrepancy found (hash-index.db state, §2) and
  flagged rather than resolved.
- **2026-08-17 session (third pass, this chat thread, Grey re-uploaded
  the document for review):** Re-checked project knowledge against this
  document's own claims rather than adding new investigation. Surfaced
  two stale "at-risk" flags that had already been resolved days earlier
  but never folded in here (`neardupe.py` `TITLE_THRESHOLD`, `progress.py`
  git-tracking, §2); two open items that existed in source material but
  weren't yet listed here (`Various Artists` rescan, API-key-entry menu,
  §7); one item where a partial fix's scope needed sharper wording so it
  isn't mistaken for a full fix (`config.py`'s lossy-extension sets
  themselves vs. `transcode.py`'s now-fixed consumption of them, §7);
  and added the specific operational detail behind the existing Nemo
  reference (§4.5) and the name of the fingerprint tool behind the canon
  spreadsheet (§7). No new code investigation happened in this pass — it
  is a documentation-completeness pass, not a findings pass, and nothing
  above should be read as newly discovered rather than newly recorded.
- **2026-08-17 session (fourth pass, this chat thread, Claude in
  chat):** Structural cut, per Grey's approval: §7 replaced with a
  pointer at the new `MUSAEUS_OPEN_ITEMS.md`/`MUSAEUS_TODO.md` disposable
  docs (§2's still-open items point there instead of restating status);
  removed the fully-resolved `neardupe.py`/`progress.py` note from §2
  (already covered in `MUSAEUS_OPEN_ITEMS.md`). Confirmed directly
  against `musaeus/stages/__init__.py` that `DEFAULT_PIPELINE` runs
  `ArtistConsolidateStage` (Act 1) before `CrossDupeStage`/
  `DupeResolverStage` (Act 2) — settles the pipeline-stage-order question
  for the artist-name sense of "canon" (§2). Added §4.15, a standing rule
  against using bare "canon" as shorthand, after nearly misreading that
  same claim as being about `CanonicalizeStage` instead. Also cleaned up
  the DOCUMENTATION folder: deleted confirmed byte-identical duplicate
  files and, per Grey's explicit call, superseded drafts of this scope
  doc, `MUSAEUS_HANDOFF_SUMMARY`, and the `2026-08-15` session handoff
  family (kept `MUSAEUS_PROJECT_SCOPE (2).md` and
  `MUSAEUS_SESSION_HANDOFF_2026-08-15 (3) (copy).md` as canonical).
- **2026-08-17/18 session (Claude Code, this thread):** Reordered
  `DEFAULT_PIPELINE` — `PermissionsStage` into Act 1, `Enrich`/`MBEnrich`
  to default-on positioned last (§2, §5) — then, after Claude(chat)
  challenged the motivating case and it was confirmed against
  `sentinel.py`'s own docstring that cross-format duplicate detection
  already works via PCM-based `audio_hash`, reverted the separate
  same-day move of `CanonicalizeStage` ahead of dedup (§5). Fixed the
  Enrich/MBEnrich dry-run network-call gap Claude(chat) flagged as
  higher-stakes once both joined the default chain (§5). Found and fixed
  two pre-existing bugs surfaced by exercising these stages through the
  full pipeline for the first time (`EnrichStage`/`NearDupeStage` bypassing
  dependency-injected config; `MBEnrichStage` hard-failing on network
  loss). Confirmed via direct `git diff` (not just re-reading current
  state, per Grey's explicit request) that an incident-scarred
  `finally:`-block cleanup pattern in `console.py` survived a
  `try`/`except`/`contextlib.suppress` refactor intact. Corrected §2's
  pipeline-stage-order entry, which had gone stale for about a day
  describing `Permissions`/`Enrich`/`MBEnrich` as on-demand-only after
  they'd already joined the default chain — the fourth confirmed instance
  of "fixed in code, never folded into the doc," now codified as §4.16.
  Also traced and triaged real CI failures on the pre-push-hook PR:
  fixed everything attributable to this session's own commits (a real
  mypy gap from an uncommitted dependency, several lint nits), confirmed
  the remaining failures are ~30 files of pre-existing debt unrelated to
  this session, present in CI runs from 9 days earlier.
- **2026-08-18 session (Claude Code, this thread, continued):** CI
  confirmed fully green on `chore/add-pre-push-hook` (typecheck, lint,
  test 3.10/3.11/3.12 all passing). Wrote, smoke-tested (isolated scratch
  vault, not the real one), committed, and pushed
  `scripts/musaeus_migrate_to_archive.py` (commit `28fc237`) per Grey's
  explicit go-ahead. Coordinated with a second Claude Code session
  running Phase 2A/2B build work in parallel (per the
  `MUSAEUS_PHASE2_HANDOFF_2026-08-18.md` brief): confirmed an ffmpeg
  loudnorm process it observed was Phase 1's existing `ForgeStage`/
  `AuditStage` doing normal read-only ReplayGain measurement, not a
  write-back or anything Phase-2-related; that session in turn corrected
  this doc's understanding of the Phase 2A/2B build shape — see §5, both
  are standalone scripts, not new pipeline Stages, contradicting the
  original handoff brief's `BaseStage` instruction.
- **2026-08-18 session (Claude Code, this thread, continued further):**
  The overnight run (started 08:59, `run_20260818T155951Z_49b7af`)
  completed — Enrich/MBEnrich both `OK`, process exited normally — but
  `AuditStage` flagged 10,415 problems, correctly refusing to report
  success rather than crashing. Root-caused directly from the code (not
  just the log): a real bug in `AuditStage`'s hash-index check requiring
  an exact `(audio_hash, file_path)` match against a table whose
  `file_path` is documented as a point-in-time snapshot, never kept in
  sync after `DupeResolverStage` moves a row. Fixed, tested, committed
  (`54fd880`), then re-ran `musaeus audit` directly against the real
  vault (read-only) to confirm: `AUDIT PASSED`, all 11,800 finalized rows
  confirmed in the hash index. See §5 for full detail. Also reviewed,
  independently re-verified (pytest/ruff/mypy, not just trusted the
  second session's own report), and committed+pushed that session's
  Phase 2A/2B work (`c25240e`), including bringing the previously
  entirely-uncommitted `scripts/car_library/` toolchain into git for the
  first time. Began investigating the known 12,242 dupe-resolver "file
  missing on disk" errors now that the overnight run has finished (per
  Grey's explicit go-ahead once both blocking conditions cleared):
  found, but did not yet fix, a related staleness issue in the
  `duplicates` table (422 pending rows, 0 of 370 distinct file_paths
  found on disk) — see §5. Coordinated with the second session on its
  own next phase (Phase 3, USB wipe): no action needed, they cleared the
  destructive-operation safety-gate design (typed device path + size,
  plus an independent denylist check) with Grey directly before writing
  anything.
- **2026-08-18 session (second Claude Code session, continued):** Built
  Phase 3 (`scripts/usb_transfer/transfer_to_usb.py`) per the safety-gate
  design cleared with Grey beforehand — see §5 for full detail. Caught
  and fixed its own fail-open-instead-of-fail-closed denylist bug before
  shipping (own test suite, not a later review). A real read-only device
  check surfaced that `/dev/sdb` is this machine's actual 8TB backup
  drive, not a spare test device — added `--extra-critical-mount` as a
  direct result. Flagged the overnight run's completion (and its initial
  Audit failure) to the first session as soon as noticed; confirmed
  already root-caused, fixed, and re-verified against the real vault by
  the time of that message (§5's audit entry). 419/419 passing, ruff/mypy
  clean. **Committed by the first session, `46d3da8`** — reviewed
  independently (read the full script, spot-checked the safety-critical
  tests) before landing, same pattern as Phase 2A/2B.
- **2026-08-18 session (first Claude Code session, continued further):**
  Grey approved and this session implemented the proposed
  `DupeResolverStage` fix for the 422-row `duplicates`-table staleness
  found in the previous pass (commit `a9a31f5`): before recording "file
  missing on disk" as an error, check the `events` log for a matching
  `DUPE_MOVED_FOR_REVIEW` entry proving a *different* group/run already
  resolved the same physical file; if found, mark the row `archive`
  instead of re-erroring forever. **Verified live against the real
  vault, not just in tests:** ran `musaeus dupe-resolver` for real (Grey's
  explicit go-ahead) — 422 pending rows dropped to 1 on the first run
  (`archive` status count 17,247 → 17,668, exactly matching); the one
  row left is a literal test-artifact filename
  (`luthor king - Test with featured artist title.m4a`), never created
  on disk, unrelated to the pattern. Ran `musaeus permissions` against
  real (non-empty, 3-file) `INBOX` content for the first time — clean
  pass, but the actual repair logic remains genuinely unexercised since
  all 3 files already had correct permissions and deliberately creating
  a dirty-permission test file was blocked by the session's own
  permission classifier (reasonable — real, if temporary, corruption of
  a real file). Investigated Claude(chat)'s separate finding that
  `FinalizeStage` still writes to `ALAC-Library`, never `ALAC_Archive` —
  confirmed directly in code (`finalize.py`'s `_target_path()` uses
  `ctx.alac_library`; `config.py` has no `alac_archive` at all). Decided,
  with Grey, not to repoint Finalize now (real Phase-2A-cutover work,
  would also require reworking `AuditStage`'s Check 1/2, unnecessary
  risk mid-session on the same DB touched twice already) — instead added
  a non-blocking warning to `build_alac_library.py` itself, checking for
  finalized rows never migrated to `ALAC_Archive` (commit `c8626e5`).
  Confirmed live: flags exactly 1,385 unmigrated rows, matching the
  overnight run's own `CATALOGUED` count. Operational takeaway: re-run
  `musaeus_migrate_to_archive.py` before each Phase 2A pass; no code
  change to the live pipeline needed. Then ran Phase 1 end-to-end for
  real (`musaeus run`, Grey's explicit request) against current `INBOX`
  content (3 files — no `--limit` flag exists on `run`, and 3 was the
  entire available set, so "all files" and "a select group" were the
  same thing this time).
- **2026-08-19 session (second Claude Code session):** New assignment
  from a full 222-script ORPHEUS audit the other session ran overnight,
  four real gaps found and split by ownership. Built and vendored
  `orpheus_noise_generator.py` (§6) — the natural sibling to the
  already-vendored `orpheus_noise_masker.py`. Only real ORPHEUS
  dependency (`lib.orpheus_paths.RUNS_ROOT`, used solely to compute the
  output dir) removed entirely; now reads `ORPHEUS_NOISE_DIR` from the
  environment, matching `noise_masker.py`'s own established override
  pattern. New thin wrapper `scripts/car_library/generate_noise.py`
  points it at `<vault_root>/RUNS/Noise`, the same location
  `build_car_library.py`'s masking step and `curator.py` already expect
  noise tracks to live — nothing downstream needed to change. Confirmed
  working via real execution (not just import): a real ffmpeg two-pass
  loudnorm pipeline, short duration override for test speed, output
  verified via ffprobe and re-measured loudness (-16.1 LUFS vs. -16.0
  target). 4 new tests, full suite 486/486, ruff/mypy clean. Not yet
  committed.
- **2026-08-19 night into 2026-08-20 (first Claude Code session):**
  Closed out the remaining three of the four 222-script ORPHEUS-audit
  gaps split with the second session — tribute-quarantine, various-
  artists-fix, bit-rot (§6) — each following the established
  read-the-source-in-full-then-port-faithfully discipline, real-vault-
  verified read-only before committing. Reviewed and committed the
  second session's noise-generator work (`8c06409`) using the same
  independent-verification standard applied to this session's own work.
  Corrected the bit-rot design mid-session once live-vault testing
  disproved the first version's premise (§5). Added a Forge tag-read
  shortcut and a BPM multichannel-audio permanent skip, both found
  necessary through direct use rather than planned in advance (§5).
  Corrected the pipeline-placement decision for `PermissionsStage`
  (already wired into `DEFAULT_PIPELINE` since 2026-08-17 — an earlier
  phrasing about the console menu was misread as "not in the pipeline,"
  clarified) before wiring BPM and VariousArtistsFix in for real (§5).
  Accepted a correction from Grey mid-session on BPM's own cost/benefit
  framing (tag-shortcut + resumability mean the real cost is "once per
  new file," not "every run") and reversed the initial recommendation
  accordingly, rather than defending the original position. Diagnosed
  and fixed a live DB-vs-disk staleness problem caused by a hard reset
  (§5) — including catching that the obvious-looking fix
  (`rebuild.py`'s event-log replay) was itself stale and would have
  made things worse, not better, before it was used. Ran the first
  full-scale real-vault test of the reordered pipeline (§5): a genuine
  overnight run, a real OOM kill, a real kill+restart, and end-to-end
  confirmation that resumability holds under an actual crash, not just
  in tests — the run was still finishing (Forge stage) as this session
  ended. Full suite stayed green throughout (562/562 at last check),
  ruff/mypy clean, CI confirmed green after every push via `gh run
  list`/`gh run view`, never assumed.
- **2026-08-21 session (Claude Code, this thread):** Grey asked for a
  review of the whole project. The code itself was in good shape —
  562/562 tests, ruff and mypy clean — so every finding was *around* the
  code rather than in it. **PR #6, opened 2026-08-07 as a small chore
  (add a pre-push hook), had become the de-facto trunk for two weeks and
  68 commits**; `main` had received nothing in that time. Retitled to
  describe what it actually contained and merged, along with 13
  never-committed files — two of which (`musaeus_idle_ms.py`,
  `musaeus_notify.py`) were *required by tracked scripts*, so the repo
  was broken on a fresh clone, and 22 tests covering
  `ArtistConsolidate`/`Normalize` that CI had therefore never run. Also
  rescued two real uncommitted `transcode.py` bug fixes.
  Adjudicated 8 CodeRabbit review threads left unresolved for two weeks,
  each verified against current code rather than trusted or dismissed:
  three were real and fixed (`(the)` never converted; Roman numerals
  mangled past X — `PART XIV` → `Part Xiv`; dotted abbreviations
  destroyed — `U.S.A.` → `U.s.a.`), two were half-right (a dead
  `idle_forge` never restarting, an empty-set monitor that could never
  finish — both fixed; the `set -e` and ZeroDivisionError halves were
  not valid), one was a real doc fix, and two were stale or simply wrong
  (`De La Soul` was already guarded; the claimed `"Beatles, The (the)"`
  corruption does not occur). See §5 for the durable findings —
  single-definition article handling, `albumartist` repair, the
  audit-trail-vs-event-store distinction, and tests-assert-the-
  implementation.
  Also traced three truncated artist names (`Terence Trent D'Arby` →
  `nce Trent D'Arby`, etc.) to source-tag damage rather than MUSAEUS —
  every `strip()` call in the codebase was verified correct — and
  repaired them by hand after an attempt at automated detection proved
  untrustworthy in both directions. Widened `is_various()`, which was
  exact-match-only and had silently missed every compound "Various
  Artists — …" credit. Rebuilt `MUSAEUS_OPEN_ITEMS.md`, which had
  drifted far enough to list six already-shipped features as "not yet
  built"; every item in the new revision was verified against the live
  DB and filesystem before being written.
- **2026-08-21 session (Claude Code, continued — second pass):** Grey
  pushed back on the earlier "false positive" framing and was right to:
  `Bon Jovi`/`Tina Turner`/`Bob Seger` consolidations are the documented
  **prefer-solo-name** policy (§5), not errors. This is a personal
  library organised by the artist Grey would search for, and a
  mainstream-catalogue correctness standard was the wrong lens. The one
  genuine exception noted: `Paul Young` (UK) and `John Paul Young`
  (Australian) are different people who would merge under a purely
  positional rule.
  Grey then pointed out the artist canon already existed for exactly
  this. **It did — and it had never been applied.** `organize.py`'s
  docstring claimed it used `ArtistCanon` but never imported it;
  `normalize.py` said canon lookup was Scholar/Enrich's job and neither
  imported it either; only `neardupe.py` touched it, and only for
  grouping. 34 hand-curated mappings had never corrected a single name —
  the third "documented as intended, never implemented" instance found
  in one day. Now applied as a pre-grouping pass in `ArtistConsolidate`,
  **exact-match only**: it rewrites real metadata unattended, so the
  canon file is the sole authority and no similarity score can promote a
  guess. That is also the protected-pair guarantee — `Paul Young` and
  `John Paul Young` score 80 against each other, under the 88 fuzzy
  threshold today but only by 8 points. Verified live: immediately fixed
  `Jan` → `Jan and Dean` (5 tracks), a real truncation that had sat
  uncorrected precisely because nothing read the canon.
  Built `rebuild-from-disk` as the real replacement for the disabled
  `rebuild-db`, recovering 26 of 47 columns from filesystem + embedded
  tags + ffprobe + recomputed hashes, with `--promote` renaming the old
  archive aside rather than dropping it. It works *because of* Grey's own
  resumability principle: MUSAEUS writes its results back into the files,
  so the files can answer "what is true now" even when the DB cannot.
  That tool then immediately earned its keep by exposing a live bug:
  **`ForgeStage` had been writing no M4A tags at all.** It assigned to a
  dotted key mutagen cannot serialise, so `save()` succeeded, the
  function returned `True`, and nothing was written — 12,279 `FORGE_TAG`
  events, zero tags on disk. It also corrected a misreading from the
  previous day: Forge's tag-shortcut reporting "from existing tags: 0"
  was blamed on first-time files, when the real cause was that the tags
  had never existed. One of this session's own tests from the day before
  had to be rewritten — it mocked the dotted key, so it validated the
  broken assumption and passed while no real file had the tag: the
  tests-assert-the-implementation pattern (§5) appearing in freshly
  written code, not just inherited code.
