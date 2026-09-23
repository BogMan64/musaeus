# MUSAEUS — review brief

**Range:** `main`, as of 2026-09-23. PR #14 was merged that day after 33 days
blocked, bringing 349 commits (+84,913 / −1,700) that had accumulated since
2026-08-21. It was a SQUASH merge, so `main` carries the whole change as one
commit `a173e06`; the per-commit history lives on the branch
`fix/dedupe-policy-and-permissions-sweep` if a finding needs to be dated.
This range is equivalent to "from the `review/cxs89d-archive` tag
(2026-08-29) forward".

**Repo:** https://github.com/BogMan64/musaeus

---

## What this review is for

Do **not** do a general sweep. One was run on 2026-09-09: 19 findings, 2 real
(8 of the 19 were rejected as "no verification performed"). A general pass over
84,913 lines produces a list nobody reads.

This codebase has one recurring defect shape, and it is not "bad code". It is
**two things that must agree, quietly ceasing to agree, while both sides
individually look correct, compile, import and pass lint.**

Documented history of exactly this (from CLAUDE.md, one audit, 2026-09-02):

| concept | independent copies | consequence |
|---|---|---|
| strip a bracketed annotation | 3 | none handled `{ }` |
| article form of an artist name | 2 | the band "Healing, The" flagged as junk |
| read a file's duration | 7 | container vs stream never named |
| duration tolerance constant | 5 | 1.5 four times, 2.0 once |
| add a column if missing | 9 | identical 8-line function, 9 times |

These are Type-4 (semantic) clones: same behaviour, different text. `ruff` has
no cross-file duplicate rule; `pylint` R0801 and token-based CPD want four or
more identical lines and find none of these. There is no reliable open-source
Python tool for this category. **A human or a model reading for agreement is
the only detector.**

Two more instances were found on 2026-09-22/23 while untangling branches, which
is what prompted this brief:

1. A migration renamed the live `duplicates` table to `duplicates_legacy` and
   built a differently-shaped table under the old name, while **13 modules and
   scripts** still queried the bare name for `group_id` / `file_path` /
   `status`. The fix sat unmerged on a side branch for 13 days. Nothing was
   ever at risk in the data — the migration renames rather than drops — but
   running it would have left the dupe resolver **silently blind**.
2. A test gate (`G4`) read `PRAGMA table_info(duplicates)` on a table that had
   been renamed the following day on a different branch. It read back an empty
   column list and the branch went stale in a corner.

Both are the same shape. Neither is exotic. Neither is catchable by a linter.

---

## Three targets, in priority order

### 1. Schema vs. queries
Migrations rename and reshape tables; modules query names as string literals.
Find every place where a table or column name is written as a literal and
confirm the migration chain still produces that shape. `musaeus/state/`,
`musaeus/stages/`, `scripts/`. The known case is fixed — the question is
whether it is the only one.

### 2. `verify_effect` honesty
Contract: returning `NO_VERIFICATION` means "I did not look". Returning `[]`
means "I looked and found nothing wrong". Conflating them is what let the
AlbumArt stage report `✓verified` while every single embed failed.
For every stage with `CLAIMS_EFFECT = True`: does its `verify_effect` actually
measure the artifact, or does it return a value that merely reads like success?
**Measure the artifact, not the report** — four format bugs in three days were
invisible in the code and obvious the moment the output file was probed.

### 3. Guards that check the wrong precondition
The richest seam found on 2026-09-23, and a generalisation of targets 1 and 2.
A guard asks one question while the real failure is a different one, so it
passes and the thing it guards still breaks:

- `sleep_inhibit.py` asked "is `systemd-inhibit` on PATH". The real failure is
  "it is on PATH and returns Access denied". `os.execve` SUCCEEDS, the process
  is replaced, the inhibitor exits 1, and the work never runs -- the `OSError`
  handler could not fire because by then there was no process left to raise
  into. Its own docstring promised the tool still runs. On any host without a
  session bus, MUSAEUS refused to do anything at all.
- P0-19's G10 decided an outbound call had happened with
  `notified = "[notify]" in out`. It reads TEXT, not sockets -- the wrapper is
  a subprocess, so the transport harness never sees it. A refusal that says so
  tripped the same check as a send. A green G10 is NOT proof of network
  silence, and should not be read as such.

Look for the same shape elsewhere: `shutil.which` standing in for "it works",
a string match standing in for an observation, an exception handler for an
error path that cannot reach it.

---

## Known-and-accepted — do not re-report

- **`main` was 349 commits behind.** Cause identified: PR #14 was BLOCKED on a
  red `lint` check, so the three test jobs behind it had NEVER run, on any
  commit. Cleared and merged 2026-09-23; CI is green on all five checks.
- **Eleven CI failures, all fixed 2026-09-23**, found the moment those jobs
  could first run. Do not re-report these:
  `sleep_inhibit.py` exec-before-probe (8 of the 11); a hardcoded
  `/home/grey/.cache` in P0-19's G10, which is why that gate had never run
  anywhere but one laptop; `doctor` folding host findings into the LIBRARY
  verdict, so a missing `ifuse` reported `library integrity: WARN`;
  `musaeus_notify.py` never consulting `musaeus.network_policy` before
  dialling ntfy.sh.
- **Tests that mutate tracked files — fixed 2026-09-23 (PR #17).** P0-19
  wrote its evidence into tracked `docs/p0_evidence/`, so every `pytest`
  dirtied 11 committed files; it once blocked a `git checkout` mid-merge.
  Writing is now opt-in via `MUSAEUS_WRITE_EVIDENCE` (reads stay on the
  committed record, because G11 needs the baseline). Guarded twice:
  `tests/test_tests_do_not_write_tracked_files.py` reads source, and a CI step
  fails if `pytest` leaves the working tree changed. That second check
  measures the tree itself, so any OTHER instance now fails CI on its own.
  Do not re-report this one.
- **6 of 11 P0 gates have no CLI path** (P0-19's own finding). More than half
  the P0 safety layer is not wired to the program a user runs. Known. Worth
  confirming the count, not worth rediscovering.
- **Duplicated *judgement* is intentional.** Sharing the mechanism is right;
  sharing the judgement usually is not. `neardupe` takes the bracket alphabet
  but keeps its own rule about which annotations are safe to strip, because
  "Here I Am (Come and Take Me)" must not collapse to "Here I Am". Each stage
  declaring its own columns is deliberate. Do not report these as duplication.
- **23 `UP031` printf-format warnings in `scripts/`.** CI lints only `musaeus/`
  and `tests/`. Converting these to f-strings is an active trap here: an
  f-string eats a regex quantifier, turning `\d{2}` into `\d2` and `\d{1,2}`
  into `\d(1, 2)`. Both compile. Leave them.

---

## Traps that compile, import, lint and lie

Every one was hit for real in this repo. If the review proposes a change near
any of these, it must say which one it is avoiding.

- An f-string eats a regex quantifier (above).
- `ffmpeg` exits 0 on a truncated file — it reports "Input buffer exhausted" on
  *stderr* and returns 0. Check `returncode == 0 AND not stderr`.
- `ffmpeg` reads stdin and eats the enclosing loop's input. Every `ffmpeg` call
  inside a `while read` loop needs `-nostdin`.
- Single-pass `loudnorm` is not the two-pass bake. One-pass `loudnorm=I=-14` is
  a dynamic normalizer; it put one file 1.5 LU hot while its peers sat at −13.9.
- Metadata cannot see truncation. In MP4 both durations live in the `moov`
  atom, written before the audio. A 30 s file cut to a third still reports 30.0.
  Only a decode knows.
- An unstated format property is inherited from the input. Sample rate, channel
  count, bit depth — this has shipped four separate bugs.
- Existence is not completeness. Write to `.part`, verify, then rename.
- `pgrep -f` matches the shell that is asking. Use `scripts/musaeus_running.sh`.

---

## How to report

For each finding: the two things that disagree, the file:line of each, and a
concrete failure — inputs or state, and the wrong output or silent no-op that
results. A finding with no stated failure path is not a finding.

Say plainly which findings were verified by running something and which were
read-only inferences. Do not mark anything verified that was not observed in a
tool result. Nine unverified guesses are worse than two measured facts.

Test command: `python3 -m pytest -q` (the suite sets `MUSAEUS_NO_IDLE_THROTTLE=1`;
without it the idle throttle SIGSTOPs ffmpeg children and tests fail by timing
out, which reads exactly like a slow disk). Baseline on `main` at `73bf027`:
**3143 passed, 1 skipped** locally; CI runs 3.10, 3.11 and 3.12 and is green
on all three. Read the summary line, not the shell exit code.
