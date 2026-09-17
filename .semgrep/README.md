# Semgrep rules for MUSAEUS

Catches a specific failure mode this project has hit repeatedly: a small
piece of text/measurement logic (bracket stripping, article handling,
reading a file's duration, adding a DB column) gets reimplemented instead
of reused, usually slightly wrong in a way ruff/pylint cannot see because
the two copies are textually different while doing the same thing.

## Run it

    pip install -e ".[dev]"   # semgrep is now a dev dependency
    semgrep --config .semgrep/rules.yml musaeus/ scripts/ tests/

Exit code is non-zero if anything fires. As of 2026-09-08 the real tree is
clean **and the command genuinely looks at all three paths** — 296 files,
160 of them from `tests/`.

That second clause is not decoration. Until 2026-09-08 this README made the
same claim while `tests/` scanned **zero files**: semgrep ships default
excludes containing `tests/`, creating a `.semgrepignore` replaces those
defaults wholesale, and `--no-git-ignore` does not lift them. 136 files were
scanned, none from `tests/`, and the passing exit code asserted coverage that
did not exist. An ERROR-severity rule had never examined the tree it actually
matches in. (M-11 in the Repair Register.)

`.semgrepignore` now re-includes `tests/` and restates the ordinary noise
directories the defaults used to supply. Fixing it also exposed
`scripts/car_library/vendor/`, hidden by the same defaults and never
reported; that stays excluded, but explicitly and with the reason written
down — vendoring is a deliberate re-implementation, and the vendored
duplication is pinned by `tests/test_car_duration_tolerance_is_one_rule.py`
rather than by this linter.

Ten matches in `tests/` are suppressed inline. Each carries
`# nosemgrep: <rule-id> -- <reason>`, and a test refuses a bare `nosemgrep`,
so every exception is a recorded decision rather than a silent gap.

`tests/test_semgrep_actually_scans_what_it_claims.py` asserts the coverage
itself, so this README cannot go back to claiming a tree it never reads.

## Not wired into CI yet

`.github/workflows/ci.yml` runs ruff, mypy, and pytest. Adding semgrep as
a required check is a bigger decision than adding a local tool — a rule
with a false positive would then block every PR — so it hasn't been added
without asking first. If you want it in CI:

    - name: Install semgrep
      run: pip install semgrep
    - name: Semgrep
      run: semgrep --config .semgrep/rules.yml musaeus/ scripts/ tests/

## Adding a rule

One rule per concept, not a general-purpose lint pass. Before writing
one: has this concept actually been duplicated more than once? A rule
for a one-off is noise. Each existing rule's `message` cites the real
incident it exists to prevent — keep that pattern, and add a fixture
pair (a firing case, a deliberate non-firing case) under `.semgrep/tests/`
so the rule's own correctness is checked, not assumed.

`pattern-regex` matches raw text, including comments — a comment
*discussing* the old bad pattern can trip the rule meant to catch the
pattern itself. Describe the shape in words in comments near these rules,
not as a literal reproduction of it.
