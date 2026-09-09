# Musaeus — Full Code Review

**Date:** 2026-09-09
**Reviewed commit:** `c632ffa` (branch `main`)
**Scope:** entire repository — 121 Python files, 35,821 LOC
**Reviewer:** Kiro (Claude Opus 5)
**Purpose of this document:** a verification-ready handoff. Every finding below was confirmed by reading the code and/or running a command, and each carries a reproduction step so a second agent can independently confirm or refute it before changing anything.

---

## How to use this document

Each finding has a stable ID (`P0-A`, `P1-C`, …), a location, a falsifiable claim, and a **Verify** command. Recommended workflow:

1. Run the **Verify** step and confirm the claim independently. Do not take this document's word for it.
2. If confirmed, apply the fix.
3. If refuted, mark the finding `REFUTED` with the evidence, and say so — a wrong finding in this report is more damaging than a missing one.

**Do not bulk-apply these fixes.** The P0 items touch the code that deletes audio files. Each needs its own regression test written *first* (a test that fails before the fix and passes after).

---

## Verified baseline (facts, not opinions)

Established by running the following on the review commit with Python 3.12.13:

| Check | Command | Result |
|---|---|---|
| Test suite | `pytest tests/ -q` | **582 passed, 49 skipped** in 4.25s |
| Lint (CI scope) | `ruff check musaeus/ tests/` | **All checks passed** |
| Format (CI scope) | `ruff format --check musaeus/ tests/` | **98 files already formatted** |
| Types (CI scope) | `mypy musaeus/ --ignore-missing-imports` | **Success: no issues in 56 files** |
| Lint `scripts/` | `ruff check scripts/` | **28 errors** — `scripts/` is *not* linted by CI |
| Coverage | `pytest --cov=musaeus` | **48% overall** |

The green CI is real but shallow — see `P1-G` (CI never installs ffmpeg) for why the passing suite does not protect the dangerous code.

---

## Overall assessment

This is a **well-architected project with a serious and specific safety gap**, not a messy one.

**Genuine strengths — keep these:**

- The stage abstraction (`musaeus/stages/base.py`) is clean and correct. `BaseStage.execute()` converting exceptions into a failed `StageResult` plus a structured JSON failure report under `runs_root/FAILURES/` is a genuinely good design, and `_write_failure_report` correctly refuses to let a reporting failure mask the original error.
- `RunContext` (`musaeus/context.py`) gives one run one connection, one `run_id`, no globals. The `record_stage`-owns-the-commit rule is a sound invariant.
- `db.py:upsert_archive` — the `if f in row` guard on the `ON CONFLICT` SET clause is a real bug fix, correctly reasoned and correctly documented. This is the kind of care the whole repo should have.
- `snapshot_db_before_wipe` (`db.py:389`) uses sqlite3's **backup API** rather than a file copy, explicitly because WAL means recent commits may live in a `-wal` sidecar. That is exactly right and most people get it wrong.
- The `LOSSLESS_CODECS` frozenset (`config.py`) with its comment explaining why extension-based lossless detection is broken for `.m4a` (ALAC vs AAC) shows real domain understanding.
- Reversibility as a design principle — `DUPES_MOVED_FOR_REVIEW`, `TRIBUTE_REMOVED_FOR_REVIEW`, manifests plus generated restore scripts, "never delete, always relocate." The intent is correct.
- 582 real tests. `tests/disposable_vault.py` and the `test_p0_01_characterization.py` / `test_p0_02_dry_run_guard.py` pair show a team that writes characterization tests before changing behaviour.

**The core problem:** the codebase's *safety intent* is documented far more thoroughly than it is *implemented or enforced*. There is a recurring pattern where an extensive comment asserts a safety property that the adjacent code does not actually provide. The clearest example is `canonicalize.py:546-556`, where a three-line comment states the original is removed only after the DB write is confirmed — and the `unlink()` executes while that transaction is still open.

**A structural observation.** The docstrings in this repo are unusually long and are written as a changelog — dates, who decided what, what was reverted and why. That has real value, but it has become load-bearing: several safety claims exist *only* in prose, with no test and no assertion enforcing them. Prose cannot fail. A test can. The single highest-leverage change to this codebase is not any individual fix below — it is converting the safety claims in the comments into executable assertions.

---

# P0 — Data-loss risks

These can destroy or orphan irreplaceable audio. Fix in this order.

## P0-A — `_verify_conversion` fails **open**, then the lossless original is deleted

**Location:** `musaeus/stages/canonicalize.py:143-172` (`_verify_conversion`), consequence at `:549-550`

**Claim.** Post-conversion verification silently passes when either duration is unavailable, after which the source is unlinked.

```python
src_dur = _duration(src_probe)
out_dur = _duration(out_probe)
if (
    src_dur is not None
    and out_dur is not None
    and abs(src_dur - out_dur) > _DURATION_TOLERANCE_SEC
):
    raise CanonicalizeError(...)
```

If `src_dur` or `out_dur` is `None`, **no comparison happens and the function returns success.** `_duration()` reads only `probe["format"]["duration"]` with no fallback to `streams[0]["duration"]`, so any source whose container reports no duration (raw/broken-header WAV or AIFF, some FLAC-in-Ogg, streams carrying duration only at stream level) degrades verification to "the output has ≥1 audio stream." A 2-second truncated ALAC output passes. Then `canonicalize.py:549-550` runs `Path(old_path).unlink(missing_ok=True)` on the FLAC/WAV original.

Two further gaps in the same function:
- Both the module docstring (`:63-67`) and the function docstring (`:146-148`) claim stream **count** is compared. It is not — only `if not out_audio_streams`. Channels, sample rate, and bit depth are never compared either, so a wrong-stream `-map` or a silent downmix is invisible.
- `_DURATION_TOLERANCE_SEC = 1.5` (`:90`) means an encode truncated by up to 1.5s always passes. For a CONVERT (pure codec swap) the duration should match to ~0.05s; 1.5s is only defensible for a lossy TRANSCODE.

**Verify:**
```bash
sed -n '143,172p' musaeus/stages/canonicalize.py   # confirm the None-guard short-circuit
sed -n '546,551p' musaeus/stages/canonicalize.py   # confirm the unlink
```
Repro: stub `_probe_streams` to return `{"format": {}, "streams": [{"codec_type": "audio"}]}` for the output and assert the source survives. It will not.

**Fix.** Make it fail-closed: raise `CanonicalizeError` when either duration is `None`. Add a `streams[0]["duration"]` fallback in `_duration()`. Compare audio-stream count, channels, and sample_rate. Tighten tolerance to ~0.05s for CONVERTED/PASSTHROUGH, keep 1.5s only for TRANSCODED.

---

## P0-B — `unlink()` before `commit()` in both file-destroying stages

**Location:** `canonicalize.py:549-556`; `finalize.py:401-419`

**Claim.** The source file is deleted while the DB transaction that redirects the row is still open, so a crash in that window loses the file from the DB's point of view.

`canonicalize.py`:
```python
# Only now, with the DB row safely pointing at the verified
# STAGING copy, is it safe to remove the pre-conversion
# original -- never before the DB write is confirmed.
if new_path != old_path:
    Path(old_path).unlink(missing_ok=True)
...
if i % _COMMIT_EVERY == 0:      # _COMMIT_EVERY = 25
    ctx.conn.commit()
```

The `UPDATE` at `:503` is **not committed** when the `unlink` at `:550` runs — it commits at `:556` only when `i % 25 == 0`. For up to 24 rows at any moment the original is deleted against an uncommitted transaction. SIGKILL/OOM/power loss ⇒ rollback ⇒ `archive.file_path` still points at the now-deleted INBOX path, `canonicalized_at` is NULL, and the verified STAGING file is an orphan nothing references. **The comment states the opposite of what the code does.**

`finalize.py` is the same shape and worse: for a CONVERTED/TRANSCODED row the `source` being unlinked at `:403` is the STAGING file, which is *the only copy in existence* (canonicalize already deleted the INBOX original).

Compounding: `PRAGMA synchronous=NORMAL` (`db.py:227`) means even a *committed* WAL transaction is not durable across power loss. Every "commit before unlink" argument in this repo silently assumes `FULL`.

**Verify:**
```bash
grep -rn "fsync\|SAVEPOINT\|rollback" musaeus/     # expect: no hits
grep -n "synchronous" musaeus/db.py                # expect: NORMAL
sed -n '546,557p' musaeus/stages/canonicalize.py
sed -n '399,420p' musaeus/stages/finalize.py
```

**Fix.** `ctx.conn.commit()` immediately before each `unlink()` (or collect `old_path`s, commit once, then unlink the batch). Consider `PRAGMA synchronous=FULL` for the destructive stages specifically.

---

## P0-C — `finalize` verifies the only copy by **size alone**, with no fsync before rename

**Location:** `musaeus/stages/finalize.py:102-132` (`_copy_then_verify_then_swap`)

**Claim.** Three independent weaknesses in the function that creates the canonical library copy:

1. **Size-only verification** (`:120-125`): `src_size != tmp_size` and nothing more. No hash, no re-probe. `archive.audio_hash` is already selected onto the row by `_get_pending` and is never used. A silently corrupted copy — bad cable, failing USB enclosure, filesystem bug — has the correct size and passes.
2. **No fsync before rename** (`:127`): `shutil.copy2` then `tmp_target.rename(target)`. Python's `close()` flushes to the OS, not to disk. Power loss after the rename and after `source.unlink()` leaves a zero-length or partial `target` and no source. This is the classic durable-rename bug, applied here to the only copy.
3. **`Path.rename` silently overwrites** on POSIX. Guarded upstream by `unique_path()`, but that is TOCTOU: stat → mkdir → copy2 → rename. Two concurrent `musaeus` invocations, or anything else writing into ALAC-Library, and one file overwrites another with no error.

**Verify:** `sed -n '102,132p' musaeus/stages/finalize.py` — confirm only `st_size` is compared and no `os.fsync` appears.

**Fix.** `os.fsync()` the temp file descriptor before rename, plus fsync the destination directory fd. Verify by recomputing the audio hash against `row["audio_hash"]` rather than comparing sizes.

---

## P0-D — Three stages leave a moved file with a stale DB row (unguarded `IntegrityError`)

**Location:** `dupe_resolver.py:422`; `tribute_quarantine.py:264`; `corrupt.py:264`

**Claim.** `archive.file_path` is `UNIQUE` (`db.py:41`), so every `UPDATE archive SET file_path=...` can raise `sqlite3.IntegrityError`. Only `canonicalize`, `finalize`, and `organize` catch it.

```bash
$ grep -rln "IntegrityError" musaeus/stages/
musaeus/stages/canonicalize.py
musaeus/stages/finalize.py
musaeus/stages/organize.py
```

In the other three, the `UPDATE` follows a completed `shutil.move` and is protected only by `except OSError` — and **`sqlite3.IntegrityError` is not an `OSError`.** `corrupt.py:250-269` is the clearest case: `shutil.move` succeeds inside the `try`, the `UPDATE` raises, and the handler cannot catch it. Result: the file is physically in the quarantine directory while `archive.file_path` still points at its old location with `status='CATALOGUED'` — **a lost file** — and the exception aborts the stage so all remaining rows go unprocessed. `record_stage()` in `base.py:186` then commits every *earlier* row's update, leaving a half-applied state with no rollback.

`unique_path()` only checks the filesystem, never the DB, so a stale archive row owning that exact path is a realistic trigger.

**Verify:**
```bash
grep -rln "IntegrityError" musaeus/stages/    # 3 files, not 6
sed -n '250,270p' musaeus/stages/corrupt.py   # OSError-only around move+UPDATE
sed -n '255,268p' musaeus/stages/tribute_quarantine.py
sed -n '406,426p' musaeus/stages/dupe_resolver.py
```

**Fix.** Wrap all three `UPDATE`s in `try/except sqlite3.IntegrityError`, and on failure move the file **back** to its original path before recording the error — mirroring `finalize.py:361-388`, which already does this correctly.

---

## P0-E — `dupe_resolver` ignores `duplicates.status` and can move the last surviving copy

**Location:** `dupe_resolver.py:132-148` (`_get_group_members`), `:210-224` (`_pick_keeper_and_losers`)

**Claim (two distinct bugs).**

**1. `dup_status` is selected and never read.**
```bash
$ grep -n "dup_status" musaeus/stages/dupe_resolver.py
137:        SELECT d.file_path, d.duplicate_type, d.confidence, d.status AS dup_status,
```
One hit — the SELECT. `_get_pending_groups` finds groups having *at least one* `pending` row, but `_get_group_members` returns **all** members regardless of status, and `_pick_keeper_and_losers` takes `members[0]` as keeper.

Concrete failure: group = {A: mp3 320k, B: flac}. B was already moved to review in a prior run (`duplicates.status='archive'`), A is the survivor. `_keeper_sort_key` ranks lossless first ⇒ `keeper = B` (a path that no longer exists) ⇒ `losers = [A]` ⇒ **A, the only remaining copy, is moved out of the library.** Recoverable via manifest, but it is precisely the outcome the stage claims is impossible.

**2. The single-member rule is ungated.**
```python
if len(members) == 1:
    return None, members  # CROSS_BATCH: nothing to keep, incoming file moves
```
The docstring justifies this for `CROSS_BATCH`, but there is **no `duplicate_type` check** — this runs for every group. Any group whittled down to one row moves that last row out.

Minor, same function: `_get_live_exact_clusters` has no deterministic tiebreak, so for two byte-identical files the keeper depends on SQLite row order. Not data loss, but runs are non-reproducible.

**Verify:** the `grep -n "dup_status"` above (one hit proves it is dead), then `sed -n '210,224p' musaeus/stages/dupe_resolver.py`.

**Fix.** Filter `_get_group_members` to `dup_status = 'pending'`, or skip members whose file no longer exists. Gate the single-member branch on `dtype == 'CROSS_BATCH'`. Add `file_path` as a final tiebreak in `_keeper_sort_key`.

---

## P0-F — `organize` is unscoped: a latent mass-move of the library into INBOX, currently masked by a crash

**Location:** `organize.py:322-333` (query), `:392` (target), `:401-405` (the masking bug)

**Claim.** The query selects **all** `status='CATALOGUED'` rows with no path scoping:

```sql
SELECT id, file_path, artist, album, title
FROM archive
WHERE status = 'CATALOGUED' AND artist IS NOT NULL AND title IS NOT NULL
```

`FinalizeStage` never changes `status` — it only sets `file_path` and `finalized_at` — so after a successful `musaeus run`, **every finalized file in ALAC-Library is still `status='CATALOGUED'`.** `_organize` then builds `target_dir = ctx.inbox / artist_safe / album_safe` unconditionally. So `musaeus organize` would rename the entire finalized library back into INBOX.

It doesn't — only because of a second bug:
```python
logger.info(
    "[organize] move    %s\n                    → %s",
    current_path.relative_to(ctx.inbox),
    target_path.relative_to(ctx.inbox),
)
```
Logger args are evaluated eagerly, this block sits *before* `if not dry_run:`, and `Path.relative_to` raises `ValueError` for any path outside INBOX. The uncaught `ValueError` escapes to `base.py:181`, aborting the stage in both `run()` **and** `dry_run()`.

Net today: `musaeus organize` after a completed batch always crashes on the first finalized row. **Anyone who "fixes" the logging line without also scoping the query converts a crash into catastrophic data movement.** It also makes `dry_run()` unusable for exactly the population it needs to preview.

**Verify:** `sed -n '322,334p' musaeus/stages/organize.py` (no path predicate) and `sed -n '399,406p' musaeus/stages/organize.py` (eager `relative_to` before the dry-run gate). Confirm `OrganizeStage` is absent from `CANONICAL_PIPELINE` in `stages/__init__.py` — it is standalone-only, which is what keeps this latent.

**Fix.** Scope the query to `finalized_at IS NULL` (preferred — status is unreliable here) **before** touching the logging line. Compute relative paths defensively.

---

## P0-G — `corrupt` physically quarantines on a filename heuristic, with no manifest and no apply gate

**Location:** `corrupt.py:111-161` (`check_file`), `:240-269` (the move)

**Claim (three parts).**

1. **The documented safety flag does not exist.** The module docstring says files are quarantined "(if --apply)". `grep -c apply musaeus/stages/corrupt.py` → **1**, that docstring mention. `cli.py:1406-1410` calls the stage with `dry_run` as the only gate; `run()` always moves.

2. **The short-track check reads the filename, not the metadata:**
   ```python
   if declared_sec < MIN_DURATION_SEC:     # 45
       title = path.stem
       if not SHORT_OK_KEYWORDS.search(title):
           return True, "duration suspiciously short"
   ```
   `path.stem`, not `archive.title`. Any track under 45s whose *filename* lacks intro/outro/skit is declared corrupt and **physically moved out of the library**: hardcore punk, hidden tracks, jingles, album segues. No size/bytes-per-second sanity check is applied on this branch, so a perfectly healthy 40s file is treated identically to a truncated one.

3. **No manifest, no restore script.** Unlike `dupe_resolver` and `tribute_quarantine`, `CorruptStage` writes neither. The only record is the `CORRUPT_DETECTED` event, emitted *before* the move and recording only the source path — **the quarantine destination is never persisted anywhere.** Recovery means manually matching filenames in the quarantine directory.

Also `:141`: `size_bytes < expected_min_bytes * 0.15` with `flac/alac: 30_000` B/s gives a real floor of 4.5 kB/s; a quiet, highly compressible FLAC (ambient, spoken word) can legitimately fall below it.

**Verify:** `grep -c apply musaeus/stages/corrupt.py` (→1), `sed -n '149,156p' musaeus/stages/corrupt.py` (→ `path.stem`), `grep -c manifest musaeus/stages/corrupt.py` (→0).

**Fix.** Add a real `--apply` flag defaulting to off. Read `archive.title`. Require *both* signals (short **and** bytes/sec anomaly) before moving. Write a manifest + restore script matching `dupe_resolver`'s convention.

---

# P1 — Correctness, integrity, and operational failures

## P1-A — Four shipped CLI commands crash with `ModuleNotFoundError`

**Location:** `cli.py:758`, `:781`, `:810`, `:1505`

```bash
$ for m in musaeus_report musaeus_upgrade_check musaeus_spec_scout musaeus_canon_review; do
    [ -f "scripts/$m.py" ] && echo "EXISTS: $m" || echo "MISSING: $m"; done
MISSING: musaeus_report
MISSING: musaeus_upgrade_check
MISSING: musaeus_spec_scout
MISSING: musaeus_canon_review
```

All four are imported by `cli.py`, advertised in its module docstring and in argparse help. `musaeus report` — the flagship dashboard command — is one of them. Each fails with a raw traceback via the top-level handler at `cli.py:1667`.

Compounding: `scripts/` has **no `__init__.py`** and is excluded from the wheel (`include = ["musaeus*"]`), so `from scripts.x import y` only ever resolves via implicit namespace packages when CWD is the repo root. A non-editable `pip install musaeus` could never run these even if the files existed.

**Fix.** Restore or delete the four modules. Add a CI smoke job that runs `musaeus <cmd> --help` for **every** registered subcommand — that alone would have caught this.

---

## P1-B — `--dry-run` is globally disabled, but still documented everywhere

**Location:** `cli.py:246-263` (`_reject_unsafe_dry_run`), called at `:276-278`

`_run_pipeline`'s first action returns exit **2** for any `dry_run=True`. The guard itself is honest and well-reasoned — its comment correctly identifies that `cfg.ensure_dirs()` and `RunContext.new()` run before any stage, and that Enrich/MBEnrich/AcousticID make network calls before the dry-run check. **The problem is that nothing else was updated to match.**

- `musaeus dry-run` — the dedicated command — is 100% non-functional.
- `preflight --dry-run` and `audit --dry-run` are rejected despite their own help text saying "report-only, never mutates."
- Twelve `--dry-run` examples in `cli.py`'s docstring and five in `README.md` are all broken.
- `README.md:352` claims "`dry_run=True` is first-class — never a no-op."
- `musaeus_overnight.sh --dry-run` (line 40) now returns 2 from every stage ⇒ `run_stage` counts each as FAILED ⇒ the script exits 1 and **fires a real push notification**. The documented usage produces a false alarm.

**Fix.** Either implement real preview (P0-04/P0-05 per the guard's own comment) or whitelist the genuinely read-only stages, and update README + `cli.py` docstring + `musaeus_overnight.sh` so the documented interface matches reality.

---

## P1-C — The console bypasses the dry-run guard entirely

**Location:** `console.py:286`, `:339`, `:628`, `:711`, `:720`

```bash
$ grep -c "_reject_unsafe_dry_run" musaeus/console.py
0
```

The console has its own pipeline runner calling `RunContext.new(..., dry_run=dry_run)` directly. Menu option 1 is literally `"Run full pipeline  [DRY RUN]"`. So the exact behaviour the CLI declares too unsafe to permit is the **default, first, most inviting option** in the interactive UI. The guard's own comment admits this ("it does not apply to musaeus/console.py's own interactive pipeline runner") — which means the safety control is documented as bypassable rather than being made non-bypassable.

**Fix.** Route both front ends through one guarded entry point. Two front ends with divergent safety guarantees is the root cause here and in P1-D.

---

## P1-D — Console reset paths diverge dangerously from the CLI's

**Location:** `console.py:499-585` (`_reset_menu`)

**Soft reset** (`:522-556`) executes an unscoped blanket wipe of hard-won work:
```sql
UPDATE archive SET status='PENDING', audio_hash=NULL, full_hash=NULL, rg_tagged_at=NULL
```
No `WHERE`. This destroys every audio hash and ReplayGain timestamp in the library — hours of ffmpeg time, unrecoverable from the event log (`rebuild.py:1-46` documents that hashes are logged truncated). And:
- **No DB snapshot.** `snapshot_db_before_wipe` is not called on this path.
- **No TTY check**, unlike `cli.py:_cmd_reset` which explicitly refuses non-interactively. Piping a file containing `YES` into `musaeus console` executes it unattended.
- `_choose()` returns default `"0"` on EOF — and **index 0 is Soft reset, not Back.** EOF *selects* the destructive option; it is stopped only by the later `"YES"` check, which itself passes only because `_prompt`'s default is `""`. Two accidental safeties, no intentional one.

**Hard reset** (`:558-585`) is mostly correct — double `DELETE` confirmation, and `snapshot_db_before_wipe` **is** genuinely called at `:570` before the unlink loop. One gap:
```bash
$ grep -c "_clear_resume" musaeus/console.py
0
```
`cli.py:_cmd_reset` calls `_clear_resume()` at `:432`; the console does not. After a console hard reset, `~/.config/musaeus/resume_state.json` survives, so the next `musaeus run` reports "Incomplete run detected" and — with no TTY — **auto-resumes** (`cli.py:314-319`), silently skipping stages against a brand-new empty DB. Concrete and reproducible.

Also `:583`'s `except OSError` does not cover `sqlite3.Error`, so a failure inside `source.backup(dest)` leaves a partial, invalid snapshot file in `_history/` looking legitimate.

**Fix.** Factor both reset paths into **one** shared function used by CLI and console. Add TTY guard + snapshot to soft reset. Change `_choose` default to Back. Have `snapshot_db_before_wipe` unlink its partial output on failure.

---

## P1-E — `_run_pipeline` has no `try/finally`: three paths skip `ctx.finish()`

**Location:** `cli.py:292` (open), `:383` (only close)

Between them, three paths return early:
1. `:327-330` — EOF/Ctrl-C at the resume prompt → `return 0` (**exit 0 although zero stages ran**).
2. `:343-348` — `KeyboardInterrupt` inside a stage → `_save_resume(...)`, `return 1`.
3. Anything escaping `stage.execute()` or thrown by `result.summarise()`/`print`.

None call `ctx.finish()`, so **no `RUN_END` event is written** and the WAL is never checkpointed. `sys.exit` means the OS reclaims the fd, so this does not deadlock — the damage is a permanently "open" run in the event log, which matters precisely because the event log is claimed to be the source of truth.

Note the console does the *opposite* and does it correctly: `ctx.finish()` then `finally: with contextlib.suppress(Exception): conn.close()`. Double-closing a sqlite3 connection is a documented no-op, and the inline comment's reasoning about `KeyboardInterrupt` being a `BaseException` is sound. **No use-after-close bug exists.**

**Fix.** Wrap `_run_pipeline`'s body in `try/finally`, and give `RunContext.finish()` an idempotency flag so a double call cannot raise `ProgrammingError`.

---

## P1-F — `rebuild-from-disk --promote`: no dry-run, and two un-transacted DDL statements

**Location:** `cli.py:1412-1437`; `rebuild_from_disk.py:305-315`

```python
conn.execute(f"ALTER TABLE archive RENAME TO {backup}")
conn.execute(f"ALTER TABLE {table} RENAME TO archive")
conn.commit()
```

No explicit `BEGIN IMMEDIATE`, no savepoint. On Python < 3.12 sqlite3 does not open an implicit transaction for DDL, so a crash between the two renames leaves the database with **no `archive` table at all**. This is the most destructive command in the tool and it has **no `--dry-run` flag**. Separately, `sys.exit(1)` at `cli.py:1433` returns before `conn.close()` at `:1436`.

`rebuild_from_disk.py:42`'s docstring also references a `--replace` flag; the real flag is `--promote`.

**Fix.** Wrap both `ALTER TABLE`s in one explicit transaction. Add `--dry-run`. Move `conn.close()` into a `finally`.

---

## P1-G — CI never installs ffmpeg, so the dangerous code is the *uncovered* code

**Location:** `.github/workflows/ci.yml:36-49`

The test job installs only `pip install -e ".[dev,fuzzy]"`. There is **no ffmpeg install step**. Every ffmpeg-dependent test therefore skips in CI:

```
tests/test_canonicalize.py    14 SKIPPED — ffmpeg/ffprobe not available
tests/test_audit.py           11 SKIPPED — ffmpeg not available
tests/test_cross_dupe.py       8 SKIPPED — ffmpeg not available
tests/test_build_alac_...py    5 SKIPPED
tests/test_orpheus_noise...    4 SKIPPED
tests/test_forge.py            3 SKIPPED
```

Measured coverage of the modules that delete files:

| Module | Coverage | Uncovered lines include |
|---|---|---|
| `stages/canonicalize.py` | **20%** | `150-171` (**`_verify_conversion` — P0-A**), `465-569` (**`run()` incl. the unlink — P0-B**) |
| `stages/corrupt.py` | **27%** | `116-156` (**heuristics — P0-G**), `192-287` (**the quarantine move**) |
| `stages/audit.py` | 52% | `162-188` (the cross-batch hash check) |
| `approval.py` | **0%** | entire module (**incl. P1-H**) |
| `rebuild_from_disk.py` | **0%** | entire module (**incl. P1-F**) |
| `stages/sanitize.py` | 33% | `134-192` |
| `cli.py` | 18% | `832-1670` (parser + full dispatch chain) |

**This is the single most important finding in the review.** Every P0 above sits on a line that CI never executes. `test_canonicalize.py` exists and is well written — it just never runs where it matters. The 582 green tests create justified confidence in the pure-logic layers (canon, fuzzy, normalize, various_artists_fix are genuinely well covered) and **unjustified** confidence in the file-destroying layer.

**Verify:**
```bash
grep -c ffmpeg .github/workflows/ci.yml       # → 0
pytest tests/ -q -rs | grep -c "ffmpeg"       # → 46
pytest tests/ --cov=musaeus --cov-report=term-missing | grep canonicalize
```

**Fix.** Add `- run: sudo apt-get update && sudo apt-get install -y ffmpeg` to the test job. This is a two-line change that converts 46 skipped tests into real coverage of the most dangerous code in the project. **Do this before any P0 fix**, so the fixes are made against a suite that can actually validate them.

---

## P1-H — SQL identifier interpolated from a user-edited TSV

**Location:** `approval.py:299-301`

```python
conn.execute(
    f"UPDATE archive SET {entry.field_name} = ? WHERE file_path = ?",
    (entry.suggested_value, entry.file_path),
)
```

`entry.field_name` comes from `read_review_tsv` → `row.get("field_name", "")` — a column in a TSV the human is *instructed to hand-edit*. It is interpolated into SQL as an identifier with **no allowlist validation**. Values are never validated against the archive schema at any point (`grep -n field_name musaeus/approval.py` shows no validation between read and execute).

This is not a remote-attacker scenario, so it is P1 rather than P0 — but it is a genuine correctness and integrity bug. A typo silently raises `OperationalError` mid-loop after earlier rows committed; a pasted value like `status='GHOST', artist` corrupts rows wholesale. The module has **0% test coverage**, so none of this is exercised.

**Contrast — not a bug:** `sanitize.py:163` uses the same f-string shape but iterates a hardcoded literal tuple `("artist", "album", "title", "genre")`. That one is safe. Do not "fix" it. Likewise the `f"ALTER TABLE ... ADD COLUMN {col}"` calls in `db.py` and six stages all interpolate module-level constants, not input.

**Fix.** Validate `entry.field_name` against an explicit allowlist (`{"artist","album","title","genre","year"}`) and reject the row otherwise. Add tests for this module.

---

## P1-I — `finalize` can permanently poison the never-wiped cross-batch hash index

**Location:** `finalize.py:239-270` (`_index_hash_before_finalizing`), called at `:342-349` before the `UPDATE` at `:351`

The hash-index entry is written **and committed** (`hash_conn.commit()` at `:270`) before the archive `UPDATE`. The ordering is deliberate and the docstring's reasoning is sound in the crash case. But in the `IntegrityError` case the handler at `:361-388` unlinks the ALAC-Library copy at `:369` and **nothing removes the already-committed `finalized_hashes` row.** That index is documented as never wiped and only ever growing.

Consequence: `lookup_finalized_hash` keys on `audio_hash` alone, so a future batch's `CrossDupe` believes this content is already in ALAC-Library when the file was deleted. The next incoming copy gets flagged `CROSS_BATCH` and moved to `DUPES_MOVED_FOR_REVIEW` — **the track silently never enters the library.** `AuditStage` cannot detect it, because Check 3 (`audit.py:150-190`) matches on `audio_hash` only and never validates that the recorded `file_path` still exists.

**Fix.** Delete the `finalized_hashes` row in the `IntegrityError` handler, and have Audit verify path existence for index entries.

---

## P1-J — `AuditStage` is structurally blind to the artifacts an interrupted run leaves

**Location:** `audit.py:68-88` (`_scan_alac_library_files`), `:108-127` (Check 1)

Audit is the gate that decides a batch is safe to snapshot and wipe. Three blind spots:

1. It filters on `p.suffix.lower() not in AUDIO_EXTENSIONS`. Finalize's temp files are named `<name>.m4a.finalize_tmp`, whose suffix is `.finalize_tmp` ⇒ **a leftover truncated `.finalize_tmp` is invisible to Audit** — precisely the artifact an interrupted finalize leaves.
2. **No check ever inspects STAGING**, despite `canonicalize.py:85-88` asserting that a non-empty STAGING at the end of a clean run is itself a signal. `.canon_tmp` and `.FAILED_VERIFY` files are never surfaced.
3. Rows moved by `DupeResolver` (`DUPE_REVIEW`) or `TributeQuarantine` (`TRIBUTE_REVIEW`) still match Check 1's `WHERE finalized_at IS NOT NULL`, and their new paths are outside ALAC-Library ⇒ Audit reports a false failure and blocks the wipe. `_EXCLUDED_SUBDIRS` fixes only the disk side, not the DB side.

Minor: `dry_run()` is identical to `run()`, and `open_hash_index` is a read-write open (it runs `mkdir`, WAL pragmas, `CREATE TABLE IF NOT EXISTS`, `commit`), so "never mutates anything" is not literally true.

**Fix.** Glob for `*.finalize_tmp`/`*.canon_tmp` explicitly; add a STAGING-not-empty check; exclude `DUPE_REVIEW`/`TRIBUTE_REVIEW` from Check 1.

---

## P1-K — `subprocess.TimeoutExpired` aborts canonicalize and skips cleanup

**Location:** `canonicalize.py:455-462`, timeouts at `:127`, `:220`, `:270`

`_process_one` catches only `CanonicalizeError` and `OSError`. `subprocess.run(..., timeout=600)` raises `TimeoutExpired`, which subclasses `SubprocessError`, **not `OSError`**. One pathological file exceeding 600s therefore escapes to `base.py:181`, leaves `.canon_tmp` behind un-renamed with no `CANONICALIZE_VERIFY_FAILED` event (`_quarantine_failed_staging` never runs), and abandons every remaining row.

**Fix.** Add `subprocess.TimeoutExpired` to the handler.

---

## P1-L — No post-move verification in any relocating stage

**Location:** `dupe_resolver.py:400`, `tribute_quarantine.py:258`, `corrupt.py:253`

All three call `shutil.move` with no post-move check. Across filesystems `shutil.move` degrades to `copy2` + `unlink(src)`; if the copy dies partway (ENOSPC on the review volume is realistic), `OSError` is caught and the source survives — but **a truncated file is left at the destination and nothing removes it.** On the next run `unique_path` sees it, so the real file lands at `... (2).m4a` and the stub remains forever, indistinguishable from a legitimately quarantined copy.

Related: in `dupe_resolver.py:532` and `tribute_quarantine.py:283`, the manifest and restore script are written **only after the whole loop**. Any mid-loop exception means every already-moved file has **no manifest and no restore script** — the recovery artifact is the first thing lost in exactly the scenario it exists for.

**Fix.** Stat the destination after the move and compare against a pre-move size. Write the manifest incrementally, or in a `finally:`.

---

# P2 — Quality, hygiene, and documentation drift

**P2-A — `progress.py` is ~95% dead code.** Only `enable_verbose_logging` has callers. `ProgressTracker`, `StageMetrics`, `get_tracker`, `record_result` and all rich-rendering helpers: zero callers (`grep` outside the module returns 0 for each). `--progress`/`--no-progress` only set `MUSAEUS_PROGRESS`, and **nothing reads it** — both flags are no-ops. `rich` and `psutil` are imported but declared in **no** dependency group, so `_HAVE_RICH` is always `False` on a clean install. Either wire it up and declare the deps, or delete ~350 lines.

**P2-B — `rebuild.py` carries ~240 unreachable lines** behind a `raise RebuildDisabledError` at `:96` and a `return 2` at `:328`, with mypy explicitly silenced via `# type: ignore[unreachable]`. Well documented, but it is dead weight that mypy is told to ignore.

**P2-C — README drift.**
- `README.md:347` — "Event log as source of truth — DB is always rebuildable" is **false and known-false**: `rebuild.py:1-46` documents at length that the event log is lossy by design and that `rebuild-db` was disabled for exactly that reason. This is the most misleading line in the docs, because it is stated as a design guarantee.
- `README.md:352` — "`dry_run=True` is first-class — never a no-op" contradicts P1-B.
- The architecture tree lists 10 stage modules; `musaeus/stages/` contains 30.
- README documents ~15 commands; the CLI implements ~70.

**P2-D — Exit-code gaps.** `_cmd_reset` returns `None` → **exit 0 whether the DB was wiped, the user cancelled, or the command refused for lack of a TTY**; a wrapper script cannot distinguish. Same for `setup` (exit 0 even when the wizard aborts), the first-run gate, `console`, and `version`. `cli.py:632-636` catches bare `Exception` around a `SELECT COUNT(*)`, reports "no review_issues table yet", and **returns 0** — a corrupt DB or lock timeout is indistinguishable from a missing table.

**P2-E — Resume state is a single global file.** `_RESUME_FILE = ~/.config/musaeus/resume_state.json` is keyed only on the stage-name list, not on vault root or DB path. Two vaults (or a test vault) share one file and can skip each other's stages — compounded by the non-TTY auto-resume at `cli.py:314-319`.

**P2-F — CI gaps beyond P1-G.** `scripts/` is neither linted nor type-checked despite being imported at runtime and listed in coverage config (`ruff check scripts/` → **28 errors**). No shellcheck for 789 lines of bash. No coverage threshold despite an elaborate `[tool.coverage.run]` block. `mypy` runs with `check_untyped_defs = false` and `disallow_untyped_defs = false`, so type checking is shallow — and `--ignore-missing-imports` is precisely what hides P1-A from CI.

**P2-G — `.githooks/pre-push` is almost certainly inert.** Git only honours it if `core.hooksPath` is set; there is no setup script or documented step (`grep -rn hooksPath` → nothing). Corroborated by merge commits on `main`. It also runs no tests or lint, so as a quality gate it provides nothing beyond branch protection that GitHub already enforces.

**P2-H — Shell script issues.**
- `idle_forge.sh:306` — `trap '... resume_forge; exit 130' INT TERM`, and `resume_forge` (`:174-201`) has **no "already started" guard**. Ctrl-C on the monitor can *start* a multi-hour forge instead of stopping one.
- `idle_run.sh:229` registers its trap before `main()` sets `RUN_STARTED=0` at `:195`; a signal in that window hits an unset variable under `set -u` — a fatal error inside the trap. Use `declare -g RUN_STARTED=0` at file scope.
- `musaeus_overnight.sh:135` — `df -BG ... | awk ... || echo 0`: the `||` binds to the pipeline and awk essentially never fails, so if `df` fails **`FREE` becomes empty**. `[[ "" -lt 50 ]]` treats it as 0 → aborts (fails safe) with the message "Only  GB free". `set -u` cannot catch it because `FREE` *is* assigned.
- `musaeus_overnight.sh:91-96` — `mkdir -p "${LOG_DIR}"` is unchecked and `exec > >(tee -a ...)` runs **before** the disk guard. On an unmounted vault both fail and the script continues with broken output redirection — the exact silent failure its header says it prevents.
- `musaeus_overnight.sh:192` — `${DRY_FLAG}` is deliberately unquoted for word splitting; works today, breaks the moment a second flag is added. Use an array.
- `musaeus_overnight.sh:169` — `pgrep -af "(bin/musaeus\b|python3 -m musaeus\b)"` will not match `python -m musaeus`, `python3.11 -m musaeus`, or a venv-absolute interpreter, undermining the lock it exists to provide.

**P2-I — `cli.py` duplication.** ~435 lines of hand-written argparse boilerplate, a ~370-line `if/elif` chain of ~70 branches, and an identical 8-line `get_config()`/`open_db()`/`try/finally` preamble repeated **eight times**. A `@with_db` decorator or an `open_db` context manager removes ~70 lines and the whole class of bug in P1-E. Also: `musaeus run --reset` (clears resume state) vs `musaeus reset` (wipes the DB) — two very different destructive semantics one word apart.

**P2-J — `tribute_quarantine` detection asymmetry.** `PROTECTED_ARTISTS` is matched with plain substring (`if protected in artist_lower`), so entries `"jan"`, `"spirit"`, `"monks"`, `"sleep"` protect huge swathes of unrelated artists ("**Jan**et Jackson", "**Spirit**ualized"). That errs safe, but `"sleep"` in PROTECTED fully dead-codes `\bsleep\b` in `JUNK_ARTIST_PATTERNS` while the album-side `\bsleep\b` still fires — inconsistent, and worth a test. Separately, `_get_candidates` is unscoped like P0-F, so running this after a batch can pull **finalized** files out of ALAC-Library — and `\btribute\b` in `JUNK_ALBUM_PATTERNS` means any legitimate album titled "A Tribute to …" is a target.

**P2-K — `dry_run()` is never DB-side-effect-free.** `record_stage()` (`context.py:120-127`) inserts a `STAGE_COMPLETE` event and commits on **every** path, including all `dry_run()` implementations and `BaseStage.execute()`'s exception handlers. Filesystem side effects *are* correctly gated in all seven destructive stages — that part holds. But the "zero mutations" contract in `base.py`'s docstring is not met at the DB layer. This is the honest technical core of what P1-B's guard is complaining about.

---

# Recommended sequence

**Phase 0 — make the tests able to see the bugs (do this first)**
1. **P1-G**: add ffmpeg to CI. Two lines; converts 46 skipped tests into real coverage of every P0 site.
2. Add a CI smoke job running `--help` for every subcommand → catches **P1-A**.
3. Lint + type-check `scripts/`; add shellcheck.

**Phase 1 — stop the bleeding (each with a failing-test-first regression test)**
4. **P0-A** — make `_verify_conversion` fail-closed. Highest single risk of destroying a lossless master.
5. **P0-B** — `commit()` before every `unlink()`, in both stages.
6. **P0-C** — fsync temp + parent dir before rename; verify by hash, not size.
7. **P0-D** — `except sqlite3.IntegrityError` + move-back in the three unguarded stages, mirroring `finalize.py:361-388`.
8. **P0-E** — honour `duplicates.status`; gate the single-member rule on `CROSS_BATCH`.
9. **P0-F** — scope `OrganizeStage`'s query *before* touching the `relative_to` line. Add a comment explaining that the crash is currently load-bearing.
10. **P0-G** — add a real apply gate to `CorruptStage`; read `archive.title`; add a manifest.

**Phase 2 — integrity and operations**
11. **P1-A** (restore/delete the four modules), **P1-E** (`try/finally`), **P1-F** (transaction + dry-run), **P1-H** (allowlist), **P1-I** (index cleanup), **P1-K** (`TimeoutExpired`), **P1-L** (post-move verification + incremental manifests).
12. **P1-C/P1-D** — unify the CLI and console behind one guarded entry point with one reset implementation. This removes a whole class of divergence.
13. **P1-B** — decide the dry-run story and make docs match code either way.

**Phase 3 — hygiene**
14. **P2-C** (README, starting with the false "source of truth" claim), **P2-A** (delete or wire up `progress.py`), **P2-D** (exit codes), **P2-I** (`@with_db`), **P2-E** (key resume file on vault), **P2-H** (shell fixes).

---

# Notes for the verifying agent

**Do not trust the docstrings in this repo as specifications.** They are extensive, dated, and describe decision history — which is valuable — but several assert safety properties the adjacent code does not implement. The clearest case is `canonicalize.py:546-548`, whose comment states the original is removed only after the DB write is confirmed, immediately above an `unlink()` that runs inside an open transaction. Verify against code, always.

**Confirmed non-bugs — do not "fix" these:**
- `sanitize.py:163` f-string SQL — iterates a hardcoded literal tuple. Safe.
- `db.py` / six stages `f"ALTER TABLE ... ADD COLUMN {col}"` — interpolates module-level constants, not input. Safe.
- `console.py:326/360/655` double `conn.close()` after `ctx.finish()` — sqlite3 documents close as idempotent, and it is wrapped in `suppress`. The `KeyboardInterrupt`/`BaseException` reasoning in the comment is correct. Safe and deliberate.
- `console.py:570` hard-reset snapshot — genuinely called before deletion. Correct.
- `finalize.py`'s hash-index-before-archive-UPDATE ordering — deliberate and correct for the *crash* case; only the `IntegrityError` revert path (P1-I) is wrong. Fix the revert, keep the ordering.
- `db.py:upsert_archive`'s `if f in row` guard — a real fix, correctly reasoned. Leave it alone.

**Reproduction hints for the P0s:**
- **P0-A**: stub `_probe_streams` to return `{"format": {}, "streams": [{"codec_type": "audio"}]}` for the output; assert the source file still exists after `run()`. It will not.
- **P0-B**: monkeypatch `ctx.conn.commit` to raise after the first `unlink`; assert `archive.file_path` still resolves to an existing file.
- **P0-D**: pre-insert an archive row owning the computed quarantine/review destination path, then run the stage; assert the moved file is still reachable from the DB.
- **P0-E**: build a two-member group with the lossless member already at `duplicates.status='archive'` and its file deleted; assert the surviving lossy member is **not** moved.
- **P0-F**: create a row with `status='CATALOGUED'` and a `file_path` under ALAC-Library, run `OrganizeStage.dry_run()`; observe the `ValueError`. Then confirm that removing the `relative_to` call causes a move into INBOX — this documents *why* the query must be scoped first.

**Single most valuable change if only one thing is done:** add ffmpeg to CI (P1-G). It costs two lines and turns 46 dormant tests into live coverage of every P0 in this report.


---
---

# ADDENDUM — 2026-09-09, after receiving the Reconstruction Document

**Read this before acting on anything above.**

Everything above reviewed `main` @ `c632ffa`, dated **2026-08-21**. That was the wrong tree. The live state of this project is `fix/dedupe-policy-and-permissions-sweep` @ `f4cb90d` (**PR #14**, open), dated 2026-09-08: **246 commits, 295 changed files, +57,636 lines**, 88,434 LOC across 314 Python files versus main's 35,821 across 121.

Nothing the Reconstruction Document describes exists on `main` — no `musaeus/state/`, no `doctor`, no `editions.py`, `artist_form.py`, `duration.py`, `protected_artists.py`, `safety/recovery.py`, no `deny_list`/`genre_validate`/`classical_composer`/`spellcheck`/`identity_tag`/`original_year` stages, no `docs/reconstruction/`. Main has 36 stage files; PR #14 has 41.

I re-ran every finding against `f4cb90d`. Results below. **Two of my P0s were already fixed — one of them exactly as I recommended.** Four survive.

## Findings that are obsolete — already fixed on PR #14

| ID | Status on `f4cb90d` |
|---|---|
| **P0-B** (unlink before commit) | **Fixed, and fixed the way I recommended.** `canonicalize.py:913` now commits per row immediately before disposing the original, with a comment describing precisely the orphan-in-STAGING failure mode I derived independently. Better than my suggestion: disposal now routes through a journalled `boundary.quarantine()` (`safety/recovery.py`, fsync-per-append) rather than `unlink()` where a checkpoint is active. |
| **P0-F** (organize mass-move) | **Fixed.** `in_non_library_area()` (`organize.py:132`) plus a per-file `dest_root` — `alac_library` for a finalized row, `inbox` for an un-finalized one — and `relative_to(dest_root)` replacing `relative_to(ctx.inbox)`. The 10,660-file figure is in the comment at `:597`. `OrganizeStage` is now wired into the pipeline (`stages/__init__.py:360`). Their fix is better than my "scope the query" suggestion: the query stays broad and the *target* resolves per file. |
| **P1-A** (4 broken CLI commands) | **Fixed.** All four `scripts/musaeus_*.py` restored, and `tests/test_cli_script_imports_resolve.py` walks the AST of every module to assert each `from scripts.X import` target exists. |

## Findings that survive on the live branch — re-verified today

| ID | Status | Evidence on `f4cb90d` |
|---|---|---|
| **P0-A** | **UNFIXED — byte-identical** | `_verify_conversion` still fails open when either duration is `None`; `_duration()` still reads only `format.duration` with no `streams[0]` fallback; stream count still never compared despite the docstring; tolerance still 1.5s. Lines `151-168` are in the **uncovered** set. |
| **P0-C** | **UNFIXED** | `finalize.py:128-129` still compares `src_size`/`tmp_size` only. `audio_hash` *is* now selected (`:228`, `:237`) but is used solely for the hash index, never to verify the copy. No fsync in finalize — the only `os.fsync` in the tree is the recovery journal (`safety/recovery.py:374`). |
| **P0-D** | **UNFIXED** | `grep -rln IntegrityError musaeus/stages/` → still exactly `canonicalize.py`, `finalize.py`, `organize.py`. `dupe_resolver`, `tribute_quarantine` and `corrupt` still guard a post-move `UPDATE` with `except OSError` alone. |
| **P0-E** | **UNFIXED** | `grep -n dup_status musaeus/stages/dupe_resolver.py` → one hit, `:284`, the SELECT. Still never read. |
| **P0-G** | **Partially fixed** | The sibling-duration comparison from §5 now exists (`corrupt.py:111`, `longest_by_song`). But `:193-194` still derives `title = path.stem` from the **filename**, and `:10` still documents an `--apply` flag that does not exist. |
| **P1-H** | **UNFIXED** | `approval.py:300` still interpolates the TSV-supplied `entry.field_name` into `UPDATE archive SET {…}` with no allowlist. |
| **P2-C** | **UNFIXED** | The claim your own §2 calls "the most load-bearing false statement in the codebase" is still in three places: `README.md:296`, `README.md:370`, `musaeus/__init__.py:12`. |

## New findings — not in my report, and not in the Reconstruction Document

### N-1 — PR #14 is red in CI and `mergeable_state: blocked`

```
$ gh api repos/BogMan64/musaeus/commits/f4cb90d/check-runs
test (3.12): failure    typecheck: failure    lint: failure
test (3.10): cancelled  test (3.11): cancelled
```

All three jobs fail. Reproduced locally:

- **lint** — `ruff check musaeus/ tests/` **passes**, but `ruff format --check` reports **142 files would be reformatted**. The head commit is *"lint: clear the 36 ruff findings in CI's scope"*; it cleared `ruff check` but CI runs `ruff format --check` as a separate step (`ci.yml:21-22`).
- **typecheck** — **19 mypy errors in 16 files.** Nine are the *same* error: `Incompatible return value type (got "_NoVerification", expected "list[str]")` in `base.py:206` and eight stages. One signature change was never propagated. Two look genuinely substantive: `sentinel.py:323` `Value of type "str | None" is not indexable`, and `console.py:397` `"type" has no attribute "NAME"`.
- **test (3.12)** — 2 failed, 2229 passed, 308 skipped. Both failures are `FileNotFoundError: 'ffmpeg'` / `'ffprobe'`: `test_alac_bake_sample_fmt.py::test_unreadable_file_defers_to_ffmpeg` and `test_albumart_undersized.py::test_min_edge_floor_rejects_a_same_size_offer` invoke the binaries with **no skip guard**, while ~275 sibling tests guard correctly. As written they cannot pass in CI.

This matters beyond the individual errors: **246 commits of careful, well-reasoned work are sitting behind a red gate on an open PR.** The Repair Register work is real and good — and none of it can merge.

### N-2 — The semgrep rules are not wired to anything

`.semgrep/rules.yml` and `.semgrepignore` exist. `tests/test_semgrep_actually_scans_what_it_claims.py` exists and asserts the rules scan the tree they claim to. Two commits are dedicated to them (`8274f21`, `6a288bf`).

```
$ grep -c semgrep .github/workflows/ci.yml
0
```

**Nothing runs semgrep.** Not CI, not the pre-push hook, not a Makefile. The rules are correct, the test guarding the rules passes, and the scanner never executes.

This is your own §5 pattern — *"a check that finds nothing is not the same as a check that found nothing wrong"* — reproduced in the newest guard in the repository. It is the exact sibling of `library files with no row: 0` beside 19 GB it could not see. The test proves the rules are well-formed; nothing proves they ever ran.

### N-3 — The ffmpeg coverage gap got 6× worse, and is now camouflaged

Skips went **49 → 308**. Grouped by reason:

```
 99  ffmpeg/ffprobe not available
 71  ffmpeg needed to mint a real m4a
 26  ffmpeg not available
 21  ffmpeg unavailable
 17  ffmpeg/fpcalc not available
 15  requires ffmpeg/ffprobe
  9  requires ffmpeg and ffprobe
  9  requires ffmpeg
  8  ffmpeg/ffprobe required -- this test exists to exercise a real conversion
```

~275 skips, **nine different wordings for one condition.** No single grep finds them; no count surfaces in CI. And `canonicalize.py` coverage is **still exactly 20%** — the file grew from 210 to 315 statements and the covered fraction did not move. `_verify_conversion` (P0-A) sits in the uncovered range on the live branch, as it did on main.

`.[dev,fuzzy]` also does not install `essentia`, so the `bpm` extra is never exercised either.

**P1-G is therefore not just still open — it is the single reason P0-A has survived two and a half weeks of intensive, high-quality repair work.** Twenty-six Repair Register findings were triaged and fixed in that window. The one class of bug that cannot be caught is the one whose tests never run.

## Where this changes my recommendations

**Supersede the sequence in the body above with this:**

1. **Get PR #14 green.** `ruff format` (mechanical), the 9 `_NoVerification` signatures (one change), the 2 unguarded tests (add the skip guard their siblings use), then look hard at `sentinel.py:323` and `console.py:397`. Nothing else matters until 246 commits can merge.
2. **Add ffmpeg to the CI test job.** Still two lines. On the live branch it converts **~275** dormant tests into live coverage, and it is what would have caught P0-A.
3. **Wire semgrep into CI** (N-2). The rules and their guard test already exist; only the invocation is missing.
4. **Standardise the skip reason** to one constant, or better, a shared `requires_ffmpeg` marker, so the coverage gap is greppable and countable.
5. Then **P0-A**, **P0-C**, **P0-D**, **P0-E**, **P0-G**'s remaining half, **P1-H**, **P2-C** — the seven findings above that survive on live code.

## On the document itself

The Reconstruction Document is the most valuable artefact in this project and its central claim is correct: the judgement is what cannot be recovered. Three of its rulings would have changed my review had I read it first — the `&`/`and` asymmetry, the deliberate breadth of tribute-quarantine, and the denylist-not-allowlist direction of the decode classifier all look like defects from the code alone, and I would have flagged at least the third.

Two places where it and the live tree disagree, both in the tree's favour:

- **§6 lists 30 stages; `f4cb90d` has 41 stage modules.** Worth a recount before the numbers are quoted again.
- **§2's "act on this" framing holds, and is stronger than stated.** I verified all four claims independently: nothing outside `musaeus/state/` calls `migrate()`, the legacy `duplicates` table has no UNIQUE constraint, `rebuild.py` is disabled, and no P0 state table exists in a live DB. Add a fifth: the false "source of truth" claim is still shipping in `README.md` **and in `musaeus/__init__.py:12`**, so it is in the package metadata a new reader meets first.

One caution the document earns the right to state and I will restate: it says *"where this document and the code disagree, read the code."* That is what this addendum did, and it is why two of my seven P0s dissolved. **I would extend it — where the code and CI disagree, run CI.** My original review's worst error was not a wrong finding; it was reviewing a branch nobody had worked on for eighteen days without first checking which ref was live. That check costs one command:

```bash
git for-each-ref --sort=-committerdate refs/remotes/ --format='%(committerdate:short) %(refname:short)'
```
