# Semgrep rules for MUSAEUS

Catches a specific failure mode this project has hit repeatedly: a small
piece of text/measurement logic (bracket stripping, article handling,
reading a file's duration, adding a DB column) gets reimplemented instead
of reused, usually slightly wrong in a way ruff/pylint cannot see because
the two copies are textually different while doing the same thing.

## Run it

    pip install -e ".[dev]"   # semgrep is now a dev dependency
    semgrep --config .semgrep/rules.yml musaeus/ scripts/ tests/

Exit code is non-zero if anything fires. As of 2026-09-03 the real tree
is clean — every finding from the first run was fixed the same session
(see `rules.yml`'s header comment for what it found on day one).

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
