# P0-19 — operational decisions still resting with Grey

Recorded, not decided. Nothing below is a recommendation to grant live
authority, and nothing below has been acted on.

## A. The three the spec's own safety rule names as unresolved

These are the inputs P0-GATE-01 needs next. All three are still open.

1. **Retention period.** How long a checkpoint, its quarantine payload and
   its operation journal are kept before they may be reclaimed.
   *Why it cannot be deferred further:* `safety/recovery.py` creates
   checkpoints and `safety/mutation.py` quarantines displaced bytes, and
   nothing in the codebase reclaims either. `CanonicalizeStage` and
   `FinalizeStage` already create real checkpoints on a real `musaeus run`
   (see `G8.txt`), so the material is accumulating today with no declared
   lifetime and no pruning policy. Against the fixed 100 GB cap, an
   unbounded retention is the thing most likely to make a future live
   proposal fail preflight for a reason that has nothing to do with the run.

2. **Rollback window.** How long after a run a rollback may still be
   invoked, and what happens to a request outside that window.
   *Extra context from this rehearsal:* there is currently no window at all
   to bound, because nothing invokes `MutationBoundary.rollback()`. Deciding
   the window is therefore also deciding whether a rollback entry point
   should exist at all (a `musaeus rollback <run_id>` command, an
   interactive prompt, or operator-only).

3. **Restoration-verification policy.** What must be proven before a
   restoration is called successful — digest equality over every restored
   item, a sample, or an operator's inspection.
   *Extra context:* `verify_checkpoint()` exists and `rollback()` refuses to
   overwrite an item that changed since the journal recorded it, so the
   machinery for a strict policy is present. What is undecided is the
   standard, and who signs off on it.

## B. Decisions this rehearsal newly surfaced

Each of these is a decision, not a bug report — the underlying findings are
in the gate files.

4. **What to do about the six unreachable subsystems.** `migrate()`,
   `append_event`, the projector, `DuplicateRepository`, `run_state.py`,
   `scheduling.py` and `reporting.py` have no live callers. Three options,
   and they have very different risk profiles:
   (a) wire them, which changes what a live database *is* and needs a
   quiet vault and a backup;
   (b) leave them and re-mark P0-06/07/09/13/14/16/17 as partial;
   (c) something in between — wire only the read-only paths.
   *Not a decision this task may make.* See `README.md`'s reachability tally.

5. **Whether `duplicates` should be renamed.** Two incompatible tables share
   that one name (`G4.txt`). Any wiring of the P0-14 contract has to resolve
   the collision first, and the two live options are renaming the new table
   or migrating the legacy one. Migrating touches a live table on a real
   database; renaming does not.

6. **Whether the `--safety-gate` should become default-on.** `cli_gate.py`'s
   own docstring explains why it is opt-in: turned on by default, the
   unattended overnight script would correctly refuse to do anything every
   night. That is the right behaviour and it needs to be adopted
   deliberately. This rehearsal has now confirmed the gate works when
   enabled (`G3.txt`, `I1-I5.txt`), which removes the technical reason to
   wait but not the operational one.

7. **Whether the scheduled run's default should change to preview.**
   `musaeus_overnight.sh` currently defaults to full execution; the module
   implementing preview-by-default is unreached (`G10.txt`). Flipping the
   wrapper's default would change what happens at 11pm tonight, so it is
   Grey's call, not a spec-task call.

8. **The two fail-open `VAULT_ROOT` defaults.** `musaeus/config.py` resolves
   the real vault when `MUSAEUS_VAULT_ROOT` is unset (via `_load_env()`
   reading `~/.config/musaeus/settings.env`), and
   `musaeus_overnight.sh:67` hardcodes the real vault as its `:-` default.
   Both are convenient and both mean "misconfigured" resolves to "the live
   library" rather than to an error. Whether to make either fail closed is a
   decision with real ergonomic cost.

9. **Whether the preview's SQLite WAL sidecars are acceptable.** `G2.txt`:
   a preview creates `musaeus.db-shm` and `musaeus.db-wal` inside the vault
   while printing that no files were written. Three ways out — open the
   preview connection with `PRAGMA query_only` on an immutable URI, accept
   the sidecars and correct the printed statement, or copy the database to a
   temp location first. The first is cheapest; the second is the only one
   that requires no code change but does require Grey to accept that
   "nothing was changed" means "nothing of substance".

## C. Explicitly NOT open, and recorded so it stays that way

- The future recovery root is exactly `/home/grey/Projects/MUSAEUS_RECOVERY`
  and the cap is exactly 100 GB (decimal, `100 * 10**9`). Confirmed as
  recorded values. The directory does not exist and was neither created nor
  probed by this rehearsal (`G3.txt`, `G11.txt`).
- The canonical vault remains read-only through P1.
- `MUSAEUS_VAULT/INBOX` is future staging scope, not current live authority.
- Nothing in this rehearsal grants live authority, and P0-GATE-01 does not
  either.
