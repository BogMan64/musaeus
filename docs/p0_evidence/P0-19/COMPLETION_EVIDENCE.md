# P0-19 — completion evidence, in the style of its neighbours in `tasks.md`

Written here rather than into
`/mnt/FORGE2TB/.kiro/specs/musaeus-consumer-readiness/tasks.md` because
another session was editing that file concurrently. Paste into `tasks.md`
under P0-19 after review.

**The checkbox is deliberately left unticked below.** Two required gates
FAIL and one is BLOCKED, and P0-19's own text says to block a
consumer-ready P0 claim unless every required gate passes.

---

- [ ] **P0-19 — Perform fixture-only P0 release rehearsal and collect exit evidence**
  - **Completion evidence:** Executed 2026-09-08 in an isolated worktree
    (`/mnt/FORGE2TB/Projects/MUSAEUS-kiro`, branch `spec/p0-19-rehearsal`) so
    it could not collide with concurrent work on
    `fix/dedupe-policy-and-permissions-sweep`. **No commit, no branch change,
    no push.** Fixture-only throughout: nothing under
    `/mnt/FORGE2TB/Projects/MUSAEUS_VAULT`, `/home/grey/Music`,
    `/home/grey/.config/musaeus` or
    `/mnt/FORGE2TB/Projects/MUSAEUS_TEST_VAULT` was read or written,
    `/home/grey/Projects/MUSAEUS_RECOVERY` was neither created nor probed and
    is confirmed still absent, no crontab was read-modify-written, no
    provider/ORPHEUS/Thunderbird/mail/Docker path was touched, and zero
    outbound connections were attempted. `tests/conftest.py`'s P0-01
    `PathGuard` and `TransportDenialHarness` stayed installed and enabled for
    the whole session and were confirmed still enabled at the end (`G11.txt`);
    neither was disabled, narrowed or skipped for any gate.

    Deliverables: `tests/test_p0_19_release_rehearsal.py` (**12 tests, 12
    passed**) and the evidence bundle `docs/p0_evidence/P0-19/` — one file
    per gate carrying verdict, coverage and reachability plus the raw
    captured output, an index mapping MCR-001–MCR-008 to gates
    (`README.md`), the open-decisions record (`OPEN_DECISIONS.md`), and the
    start-of-run protected-path baseline (`G11_baseline_start.txt`).

    **Resolved-config precondition (ran first, nothing ran before it).**
    Asserting `MUSAEUS_VAULT_ROOT` is not sufficient and was not relied on.
    The fail-open mechanism was reproduced here **without reading Grey's real
    `~/.config/musaeus/settings.env`**: a decoy `settings.env` naming a
    throwaway path was written under a disposable `HOME`, and with
    `MUSAEUS_VAULT_ROOT` unset, `MusicConfig.from_env()` resolved to that
    decoy rather than raising — because `config.py`'s `_load_env()` reads the
    file at module import time via `os.environ.setdefault()`. With no
    `settings.env` present, the documented `ValueError` does fire. So the
    documented failure path is reachable only in the situation nobody is
    actually in, and on Grey's machine the same code path yields
    `/mnt/FORGE2TB/Projects/MUSAEUS_VAULT` — the live library — per the
    dispatch brief's §2.1 reproduction, which this rehearsal cites rather
    than repeating against real configuration. Asserting the variable is
    *unset* is therefore actively dangerous, not merely insufficient. Every gate therefore asserts the four **resolved**
    `MusicConfig` fields (`vault_root`, `db_path`, `alac_library`,
    `runs_root`) land under the disposable fixture root, and aborts loudly
    naming the offending field otherwise. Every subprocess was given an
    explicitly constructed environment (never `os.environ`) and re-ran that
    same assertion **inside the child**, with the child's resolved
    `vault_root` captured into the gate that used it. Each child also
    reports which `musaeus` package it imported and is refused if it is not
    the worktree under test — this machine's editable install points at a
    different checkout, so "the CLI ran" and "the CLI under test ran" are
    not the same claim.

    **Reachability tally, which is the headline.** Of the eleven required
    gates: **3 `cli`** (G1, G2, G5), **2 `cli` but opt-in only** (G3, I3 —
    behind `--safety-gate`/`MUSAEUS_P0_SAFETY_GATE=1`), **6 `import` with no
    CLI path, i.e. UNREACHABLE** (G4, G6, G7's acquisition, G8's
    `rollback()`, I1, I2, I4), **2 `no-target`** (G9, G11), and **1 mixed
    where the delivered module is unreached and only a shell wrapper runs**
    (G10). More than half the P0 layer under test is not connected to the
    program a user runs.

    **Per-gate verdict and coverage:**
    - **G1 no external network in default preview — PASS, `cli`.** 30 stages
      driven through `musaeus dry-run` (in-process for harness observation
      and as a real child process for the CLI claim); 2 of those stages are
      network-capable (`EnrichStage`, `MBEnrichStage`); 0 connection attempts
      recorded. The zero is a
      measurement, not a vacuous truth, because network-capable stages were
      in scope. Suppression is structural: the preview routes to
      `planner.build_plan`, which never instantiates a stage.
    - **G2 before/after equality — FAIL on strict identity, PASS on
      content, `cli`.** Coverage: 10 files hashed, 6 DB tables and 5 rows
      compared, 24 directory entries walked, 2 previews run. Held: no
      existing file's bytes changed, nothing removed, DB logical-content
      checksum identical, table/row counts identical, **0 event rows
      written** — the P0-01 baseline defect (dry-run calling `ensure_dirs()`
      and committing `RUN_START`/`STAGE_COMPLETE`/`RUN_END`) is genuinely
      fixed. Failed: the preview **creates `musaeus.db-shm` and
      `musaeus.db-wal`** inside the vault root. `planner.py` opens the DB
      `mode=ro`, but the DB is in WAL mode and SQLite creates both sidecars
      even for a read-only connection. Attributed independently outside
      pytest. Meanwhile that same invocation prints
      `planner.SAFETY_STATEMENT`: "No files were written, moved or deleted,
      … and no directory was made." That sentence is false as printed, and
      MCR-001's criterion is a before/after comparison that is *identical*.
    - **G3 100 GB cap and safely-usable-space blocking — PASS for the
      primitive, INERT from the CLI, `cli` (opt-in) + `import`.** Cap read
      as exactly `100 * 10**9` (decimal, not GiB). 10 preflight checks evaluated; both blocks fired with
      `reason_code=recovery_capacity_exceeded` (cap: measured
      100,000,090,113 against a required 100,000,000,000; capacity: 1.26 GB
      safely usable against 4.26 GB required); the fixture recovery root
      was not created by the refused request; `/home/grey/Projects/
      MUSAEUS_RECOVERY` confirmed absent. **Correction to the dispatch
      brief:** there *is* a CLI path (`cli.py:389` → `cli_gate.
      enforce_execution_gate` → `run_preflight`). But
      `enforce_execution_gate(cfg, dry_run=...)` passes no `request_kwargs`,
      so `estimated_checkpoint_bytes`, `estimated_quarantine_bytes` and
      `estimated_items` are all **0** — confirmed from the child process.
      The required figure collapses to the database file size, so the cap
      check reports PASS from the CLI and can only ever block if
      `musaeus.db` itself exceeds 100 GB. The block is implemented and
      reachable; the estimate that would trip it is never computed.
    - **G4 exact AcoustID columns and insertion contract — contract PASS,
      UNREACHABLE, and incompatible with the live schema.** All 10 declared
      `INSERTION_COLUMNS` asserted by name and in order against the live
      table; 2 rows inserted; idempotent re-insert returned `None`; reject
      path exercised twice. Two independent unreachability findings:
      (i) `DuplicateRepository`/`ensure_state_tables`/`append_event`/
      `migrate` have **zero callers outside `musaeus/state/`**;
      `ensure_state_tables()` alone does not even create `duplicates` — only
      the migration chain does, so `migrate()` is the sole route and nothing
      calls it. (ii) **Two incompatible `duplicates` tables share one
      name.** `db.py`'s `_SCHEMA` defines `(group_id, file_path,
      duplicate_type, confidence, status, run_id, staged_at)`;
      `state/duplicates.py` defines `(run_id, candidate_item_id,
      matched_item_id, detector, provider_recording_id, fingerprint_digest,
      score, evidence_json, decision_status, created_at,
      evidence_identity)`. Both use `CREATE TABLE IF NOT EXISTS`, so on any
      database `db.py` touched first, `ensure_state_tables()` is a silent
      no-op and the P0-14 insert fails with
      `sqlite3.OperationalError: table duplicates has no column named
      candidate_item_id`. Asserted, so it cannot rot silently.
    - **G5 failed stage not recorded complete / not skipped on resume —
      PASS, `cli`.** 3 stages run, exactly 1 forced to fail, resume state
      read before and after both runs. The failed stage is absent from
      `completed`, re-runs on resume, and the succeeded stage does **not**
      re-run — so the gate distinguishes "retried" from "resume is broken
      outright". **Tested `cli.py`'s `resume_state.json` handling** (which
      `musaeus run` uses); **did not test `state/run_state.py`**, which has
      zero importers anywhere in `musaeus/` and whose MCR-005 logic
      (`REASON_RUN_CANCELLED`, "once cancellation is requested, no new stage
      starts") is unreachable.
    - **G6 rebuild parity — parity PASS, UNREACHABLE.** 6 canonical events
      appended and projected, 1 run and 2 stage rows compared between live
      and rebuilt projections, 0 parity differences. But a fresh
      `db.open_db()` database contains 7 tables and **0 of the 3** P0 state
      tables (`canonical_events`, `state_metadata`, `schema_migrations`),
      asserted. Building the store this gate needed took 3 migrations via
      `migrate()`, from version 0 to version 3.
      Parity is a property of an event store no MUSAEUS run writes to.
    - **G7 lock conflict — FAIL, acquisition UNREACHABLE.** 4 concurrent
      holders attempted. Identical-scope refusal holds and is a refusal not
      a delay: **0.0004 s** wall clock, from a real second process, naming
      `run-A` as owner. **But `acquire()` GRANTED a lock on
      `<vault>/ALAC-Library` while another run held `<vault>` in the same
      domain.** `scopes_conflict(a, b)` correctly returns `True` for that
      pair — the containment rule is implemented — and `acquire()` never
      consults it: it flocks `lock_dir/{scope_id}.lock`, and `scope_id` is a
      hash of root+domain, so a descendant hashes to a different file and is
      granted unconditionally. `lock.py`'s own docstring names this as one
      of the two decisions that "carry most of the weight", written directly
      out of the 2026-08-15 incident.
      `tests/test_p0_10_scope_lock.py` proves the containment rule as a
      *predicate* and proves refusal only for *identical* scopes, so its
      green result never covered this. Separately, `acquire()` has **no
      caller anywhere in `musaeus/`** — only `preflight.py:446`'s read-only
      `observe()`, itself behind the opt-in gate — so `musaeus run` takes no
      scope lock at all.
    - **G8 rollback after partial mutation — PASS for the primitive,
      `rollback()` UNREACHABLE.** 4 journalled mutations applied through the
      boundary (`write_bytes`, `move` — which journals twice, once for the
      move and once for the source release — and `quarantine`) over a 3-file
      fixture tree; the
      tree confirmed changed mid-run (so the rollback had real work);
      rollback outcome `completed`, 3 items restored, 0 failures; tree
      compared **by digest** and identical to its pre-run state; 7 residue
      files still retrievable under the recovery root (checkpoint payload,
      manifest, journal and two quarantined items). **Correction to the
      dispatch brief:** the boundary *is* wired into the running pipeline —
      `stages/canonicalize.py:794` and `stages/finalize.py:355` construct
      one, and both stages are in `ACT3_CANONICALIZE_FINALIZE` ⊂
      `DEFAULT_PIPELINE`, so a plain `musaeus run` writes real checkpoints
      and journals. But `boundary.rollback()` has no caller: nothing
      consumes them to undo a run. Both stages also honour a
      `CHECKPOINT_ENV` escape hatch that disables the boundary, and record
      "recovery boundary: UNAVAILABLE" rather than refusing when a
      checkpoint cannot be created.
    - **G9 Big Kahuna missing-root block — NO TARGET, NOT APPLICABLE.**
      100 Python files searched; **0 code matches** for
      `big.kahuna|big_kahuna|BIG_KAHUNA` (the single textual match is
      `musaeus/exports.py`'s own docstring recording the same conclusion).
      Not reported green (there is no block, so a tick would be a false
      statement) and not dropped (P0-19 names it). The surviving equivalent
      was exercised separately **under its own label** — `musaeus curator`
      with no `--export-root` and no configured root refused with exit 1.
    - **G10 scheduled run is preview / review-only — BLOCKED, plus an
      unconditional FAIL-shaped finding.** The wrapper resolved its fixture
      `VAULT_ROOT` correctly and created 3 entries under it, made **0**
      network notification attempts, and exited 0 at
      `musaeus_overnight.sh:169`'s `pgrep -af "(bin/musaeus\b|python3 -m
      musaeus\b)"` concurrency guard — the brief's §7.3 predicted this
      exactly. Live at run time: `1676041 python3 -m musaeus bitrot` and
      `1731386 …/bin/musaeus`. The preview branch was therefore never
      reached and this gate is **not** claimed green. Independent of the
      block: **the wrapper's default is not preview.**
      `musaeus_overnight.sh:79-87` sets `DRY_FLAG` only when `--dry-run` is
      passed and the documented crontab line passes no flags, so the
      scheduled default is full execution of the canonical chain. The module
      implementing preview/review-only-by-default, `musaeus/scheduling.py`,
      has **zero importers**. Also: `musaeus_overnight.sh:67`'s
      `VAULT_ROOT="${MUSAEUS_VAULT_ROOT:-/mnt/FORGE2TB/Projects/
      MUSAEUS_VAULT}"` is fail-open in the same shape as the Python side.
    - **G11 explicit no-live-data-operation audit — PASS with a qualified
      DB-size line, run last, `stat` only.** All 5 protected roots asserted;
      `/home/grey/Projects/MUSAEUS_RECOVERY` confirmed absent;
      `PathGuard.enabled` still `True` and the transport harness still
      installed at the end; **0** blocked-access attempts recorded; **0**
      connection attempts. The live database moved from 1,018,736,640 bytes
      (captured at rehearsal start, not quoted from a document) to
      1,028,497,408 bytes, a delta of **+9,760,768 bytes**. The delta grew
      between two runs of this gate minutes apart (+9,035,776, then
      +9,760,768), which is itself the evidence that the cause is the
      still-running `musaeus bitrot` job. It is distinguishable as such
      only because the vault jobs were
      enumerated at both ends. Per the brief's §3 that is a scheduling
      error, not a safety breach; the byte-size line is therefore recorded
      as qualified rather than clean.
    - **I1–I5 additionally required areas — REPORTED.** I1 fresh
      install/legacy migration/restore: **UNREACHABLE** (0 `migrate()`
      callers outside `musaeus/state/`; a live DB is unversioned). I2
      cancellation: **UNREACHABLE** — the gate refuses a post-cancellation
      mutation correctly (`MutationAfterCancellationError`), and **no stage
      passes a `CancellationGate`** to `MutationBoundary`; the CLI's actual
      cancellation is `cli.py`'s `KeyboardInterrupt` handler saving resume
      state, a different mechanism with none of MCR-005's guarantees. I3
      preflight: the one genuinely CLI-reachable area, **opt-in only**. I4
      report generation and redaction: **UNREACHABLE** — redaction works
      (path map dropped, no real path survives into shareable output), and
      `musaeus/reporting.py`'s only importer is `musaeus/scheduling.py`,
      which has none. I5 documentation consistency: **not re-audited** by
      this rehearsal.

    **Gates that contradict a mark currently `[x]` — left for Grey, not
    quietly flipped:** P0-06, P0-07, P0-09, P0-10, P0-12, P0-13, P0-14,
    P0-16 and P0-17. In every case the implementing module is present and
    its own test file passes; what the rehearsal adds is that the behaviour
    is either not reachable from the running program (P0-06/07/09/14/16/17),
    reachable but inert (P0-12), reachable for recording but not for
    recovery (P0-13), or reachable-and-defective in a way its test file's
    shape could not catch (P0-10). P0-05's preview claim is contradicted on
    the letter by G2's WAL sidecars.

    **Not covered, stated plainly:** G10's preview branch (blocked by
    concurrent vault jobs — needs a quiet-disk re-run); P0-18's CLI/
    documentation inventory (not re-audited); any execute-mode run against
    real content (out of scope and never attempted); `state/run_state.py`
    (deliberately not exercised — G5 tested the code the CLI actually uses);
    cancellation driven through a real stage (no stage accepts a gate); and
    the live DB's exact byte stability (a vault job was writing it).

  - **Live data:** **No.**
