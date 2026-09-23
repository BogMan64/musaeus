# P0-19 — fixture-only P0 release rehearsal: evidence index

**Run:** 2026-09-08, worktree `/mnt/FORGE2TB/Projects/MUSAEUS-kiro`, branch
`spec/p0-19-rehearsal`. **No commits made. No live data read or written.**

Test file: `tests/test_p0_19_release_rehearsal.py` — 12 tests, all passing.
Full suite after the addition: **2551 passed**, zero failures, 5m35s, with
`tests/conftest.py`'s `PathGuard` and transport-denial harness active. No
pre-existing test was modified and no `musaeus/` production file was
changed — `git status` shows exactly two untracked additions, this
directory and the test file.

The evidence in this directory is from the dedicated invocation
`python3 -m pytest tests/test_p0_19_release_rehearsal.py`, so that G11 runs
last as the brief requires. Running the whole suite regenerates these files
too, and in that case G11 is last within its own file but not last overall.
One file per gate in this directory, each carrying verdict, coverage,
reachability and the raw captured output including the §2.3 child-process
resolved-config line.

The headline is the reachability tally, not the pass count.

## Reachability tally

| Reachability | Count | Gates |
|---|---|---|
| `cli` — driven through the real CLI as a user would | 3 | G1, G2, G5 |
| `cli`, but opt-in only (`--safety-gate` / `MUSAEUS_P0_SAFETY_GATE=1`) | 2 | G3, I3 |
| `cli` for the wrapper, but the module the task delivered is unreached | 1 | G10 |
| **`import` with NO path from the real CLI — UNREACHABLE** | **6** | **G4, G6, G7 (acquisition), G8 (`rollback()` only), I1, I2, I4** |
| `no-target` | 2 | G9, G11 |

Six of the eleven required gates rest on code the running program does not
reach. That is the finding this rehearsal exists to produce, and it is a
success of the rehearsal rather than a failure of it.

## Verdict summary

| # | Gate | Verdict | Reachability |
|---|---|---|---|
| G1 | No external network in default preview | PASS | cli |
| G2 | Before/after DB, file, directory equality | **FAIL** on strict identity; PASS on content | cli |
| G3 | 100 GB recovery cap + safely-usable-space block | PASS for the primitive; **INERT** from the CLI | cli (opt-in) + import |
| G4 | Exact AcoustID columns and insertion contract | Contract PASS; **UNREACHABLE + incompatible with the live schema** | UNREACHABLE |
| G5 | Failed stage not recorded complete / not skipped | PASS (cli.py's resume) | cli |
| G6 | Rebuild parity | Parity PASS; **UNREACHABLE** | UNREACHABLE |
| G7 | Lock conflict | **FAIL** (descendant scope granted); acquisition **UNREACHABLE** | UNREACHABLE |
| G8 | Rollback after partial mutation | PASS for the primitive; `rollback()` **UNREACHABLE** | mixed |
| G9 | Big Kahuna missing-root block | **NO TARGET — NOT APPLICABLE** (substitute refused) | no-target |
| G10 | Scheduled run is preview / review-only | **BLOCKED** (§7.3 concurrency guard); plus unconditional finding that the default is NOT preview | mixed |
| G11 | Explicit no-live-data-operation audit | PASS, with a qualified DB-size line | no-target |
| I1–I5 | Additionally required areas | I3 reachable (opt-in); I1, I2, I4 UNREACHABLE; I5 not re-audited | mixed |

**Two required gates FAIL and one is BLOCKED.** P0-19's own text says to
"block a consumer-ready P0 claim unless every required gate passes". On this
evidence the P0 consumer-ready claim is blocked.

## MCR-001 to MCR-008 coverage map

| MCR | Gates behind it | Status |
|---|---|---|
| **MCR-001** — preview makes no change, no network | G1 (`G1.txt`), G2 (`G2.txt`), G10 (`G10.txt`) | **Partially met.** G1 holds. G2 fails on the letter: the preview creates `musaeus.db-shm` and `musaeus.db-wal` while printing "No files were written… no directory was made". G10's contribution is blocked. |
| **MCR-002** — recovery cap, authority, scope | G3 (`G3.txt`), G9 (`G9.txt`), I3 (`I1-I5.txt`) | **Met as a primitive, not as behaviour.** The cap blocks correctly when asked; the only CLI caller asks with all estimates at zero. `/home/grey/Projects/MUSAEUS_RECOVERY` confirmed still absent. |
| **MCR-003** — no permanent deletion, restorable | G8 (`G8.txt`) | **Met as a primitive.** Rollback restores the tree by digest and leaves quarantine residue retrievable. Nothing in the CLI calls `rollback()`. |
| **MCR-004** — schema, events, rebuild, duplicates | G4 (`G4.txt`), G6 (`G6.txt`), I1 (`I1-I5.txt`) | **NOT met in the running system.** All three rest on `migrate()`, which has zero callers; a live database is unversioned and has none of the P0 state tables. G4 additionally shows the P0-14 `duplicates` schema is incompatible with the legacy table of the same name. |
| **MCR-005** — failed stage, cancellation, lock | G5 (`G5.txt`), G7 (`G7.txt`), G8, G10, I2 (`I1-I5.txt`) | **Partially met.** G5 holds through the real CLI. G7 fails and is unreachable. I2's cancellation gate works and no stage passes one. |
| **MCR-006** — reporting and redaction, no network | G1 (`G1.txt`), I4 (`I1-I5.txt`) | **Met as a primitive, UNREACHABLE as behaviour.** Redaction works; `musaeus/reporting.py` is imported only by `musaeus/scheduling.py`, which has no importer, so no run emits a report. |
| **MCR-007** — documented commands and behaviour | G9 (`G9.txt`), I5 (`I1-I5.txt`) | **Not verified here.** G9 records the Big Kahuna half as having no target. The CLI inventory audit (P0-18) was **not** re-audited by this rehearsal — see "Not covered" below. |
| **MCR-008** — no live-data operation | G11 (`G11.txt`), I5 (`I1-I5.txt`), and the §2 precondition (`PRECONDITION_resolved_config.txt`) | **Met.** All five protected roots asserted untouched, the future recovery root confirmed absent, the `PathGuard` and transport harness confirmed still enabled at the end, and every subprocess's resolved config asserted inside the child. |

### MCRs with no gate behind part of their surface

- **MCR-007** has no gate behind the *documentation inventory* itself. G9
  covers one flag's absence; nothing here re-audits the published command,
  flag, default and exit-status inventory against implemented behaviour.
  That was P0-18's job and it remains hedged.
- **MCR-004**'s *restore* half (restoring from a migration backup) has no
  gate: `migrate()` creates a verified backup, but no CLI path invokes
  either the migration or a restore, so there is nothing to drive.
- **MCR-006**'s *report delivery* surface is out of P0 scope by design and
  was not exercised. No Thunderbird, mail, or SMTP path was touched.

## What was NOT covered

Stated plainly, because an honest gap is worth more than a green tick:

1. **G10's preview branch was never reached.** Two live vault jobs
   (`python3 -m musaeus bitrot`, PID 1676041, and a `musaeus` console
   process, PID 1731386) were running at dispatch. `musaeus_overnight.sh:169`
   `pgrep`s for exactly those and exits 0. The brief's §7.3 predicted this.
   G10 needs a re-run on a quiet disk.
2. **P0-18's CLI/documentation inventory was not re-audited.** Its test file
   exists and passes; its acceptance criteria were not walked one by one
   here either.
3. **No stage was driven in execute mode against real content.** G1/G2 drove
   the full 30-stage `DEFAULT_PIPELINE` in preview only. G5 used purpose-built fixture
   stages, not the real ones. So "the pipeline mutates correctly" is
   untested by this rehearsal and was never in scope.
4. **`state/run_state.py` was not exercised at all** (G5 tested `cli.py`'s
   resume instead, deliberately — see `G5.txt`).
5. **Cancellation was not driven through any stage**, because no stage
   accepts a `CancellationGate`.
6. **The live database's byte size moved during the run** (+9,760,768 bytes: 1,018,736,640 -> 1,028,497,408, and still growing between
   successive measurements) because a
   vault job was writing it. Per the brief's §3 that is a scheduling error,
   not a safety breach, and G11 records it as qualified rather than clean.
7. **No provider, network, Docker, Thunderbird, mail, crontab or ORPHEUS
   path was touched**, by design.

## Files in this bundle

- `PRECONDITION_resolved_config.txt` — the §2 precondition, and the
  reproduction showing `MUSAEUS_VAULT_ROOT` does not fail closed.
- `G1.txt` … `G11.txt`, `I1-I5.txt` — one per gate.
- `G11_baseline_start.txt` — protected-path and live-DB metadata captured
  **before** the rehearsal started, so G11 compares against a measured
  value rather than a figure written in a document.
- `OPEN_DECISIONS.md` — operational decisions still resting with Grey.
- `COMPLETION_EVIDENCE.md` — what would go into `tasks.md` under P0-19.
