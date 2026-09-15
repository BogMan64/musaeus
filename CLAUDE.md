# Working on MUSAEUS

## Before you write a small helper, look for it here

This codebase's most expensive recurring bug is not a hard one. It is
writing a five-line utility that already exists, slightly differently, and
inheriting a defect the existing copies share.

Found in one audit on 2026-09-02, all of them independent
reimplementations rather than copy-paste:

| concept | copies | consequence |
|---|---|---|
| strip a bracketed annotation | 3 | none knew `{ }`; a 4th written the same day had the same gap |
| article form of an artist name | 2 | a real band, "Healing, The", was flagged as junk for removal |
| read a file's duration | 7 | container vs stream, never named; both blind to truncation |
| duration tolerance constant | 5 | 1.5 four times, 2.0 once, same stated rationale |
| add a column if missing | 9 | identical eight-line function, nine times |

**Reach for these before writing your own:**

| need | use |
|---|---|
| strip `()`/`[]`/`{}` from a title | `musaeus.brackets.strip_bracketed` |
| what counts as a bracket | `musaeus.brackets.OPEN` / `CLOSE` |
| "The X" vs "X, The" | `musaeus.artist_form` — `natural_form`, `sort_form`, `comparison_key` |
| how long is this file | `musaeus.duration` — and read its docstring first |
| how far a duration may drift | `musaeus.duration.TOLERANCE_SEC` |
| add a column if missing | `musaeus.db.ensure_columns` |
| content identity of audio | `musaeus.hasher.audio_hash` |
| yield the machine during long work | `musaeus.idle_throttle.IdleThrottle` |
| may I reach the network | `musaeus.network_policy` |

Sharing the *mechanism* is right; sharing the *judgement* usually is not.
`neardupe` takes the bracket alphabet but keeps its own rule about which
annotations are safe to strip, because "Here I Am (Come and Take Me)" must
not collapse to "Here I Am". Each stage still declares its own columns.

## Why the linter will not save you

These are Type-4 (semantic) clones: same behaviour, different text. `ruff`
has no cross-file duplicate rule; `pylint`'s R0801 and the copy-paste
detectors are token-based and want four or more identical lines. The
research literature calls Type-4 the hardest category and has no reliable
open-source Python tool for it.

So the guard is a test. `tests/test_ensure_columns_is_shared.py::
test_only_db_may_alter_a_table` walks every module's AST and fails if
`ALTER TABLE ... ADD COLUMN` appears outside `db.py`. Write one of those
whenever you consolidate a concept — it is the only thing that catches the
next copy.

## Traps that compile, import, lint and lie

Every one of these was hit for real. None is caught by any tool here.

- **An f-string eats a regex quantifier.** Interpolating a constant turns
  `\d{2}` into `\d2` and `\d{1,2}` into `\d(1, 2)`. Both compile. Print
  the compiled `.pattern` before believing it.
- **`ffmpeg` exits 0 on a truncated file.** It reports "Input buffer
  exhausted" and "partial file" on *stderr* and returns 0. Check
  `returncode == 0 AND not stderr`.
- **Metadata cannot see truncation.** In MP4 the container and stream
  durations both live in the `moov` atom, written before the audio. A
  30-second file cut to a third still reports 30.0 both ways. Only a
  decode knows.
- **An unstated format property is decided by the input.** Sample rate,
  channel count, bit depth: if the command does not say it, ffmpeg
  inherits it. This has shipped four separate bugs — 96 kHz noise beds,
  96 kHz car audio, 24-bit inflation, and 5.1 tracks in the car.
- **A docstring is an `ast.Constant`.** An AST guard that greps string
  constants will flag prose that merely discusses the thing.
- **Existence is not completeness.** A half-written file looks finished
  and gets skipped for ever. Write to `.part`, verify, then rename.

## Verification

`verify_effect` returning `NO_VERIFICATION` means "I did not look" and
prints no seal. Returning `[]` means "I looked and found nothing wrong".
Conflating them is what let AlbumArt report `✓verified` while every embed
failed. If you add a stage that changes anything, give it a real
`verify_effect` or set `CLAIMS_EFFECT = False`.

Measure the artifact, not the report. Four format bugs in three days were
invisible in the code and obvious the moment the output file was probed.

## Tests

Run `python3 -m pytest -q`. Read the summary line, not the shell exit code
— piping through `tail` masks pytest's status.

The suite sets `MUSAEUS_NO_IDLE_THROTTLE=1`. Without it the idle throttle
`SIGSTOP`s ffmpeg children whenever someone touches the keyboard, and
tests fail by timing out — which reads exactly like a slow disk.
