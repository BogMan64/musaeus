"""The documented semgrep command must examine the trees it names.

M-11 in the Repair Register, 2026-09-08. The Register's *first* version of
this finding — "the documented command fires 9 ERROR matches" — did not
survive reproduction. The corrected version is subtler and worse.

`.semgrep/README.md` documents

    semgrep --config .semgrep/rules.yml musaeus/ scripts/ tests/

and states the tree is clean. It was clean. It was clean because the third
path scanned **zero files**: semgrep ships default excludes containing
`tests/`, and `--no-git-ignore` does not lift them. Measured that day —
**136 files scanned, none of them from `tests/`, exit code 0.**

So an ERROR-severity rule had never once examined the tree it actually
matches in, and the passing exit code asserted the opposite every time it
ran. This is the register's own recurring shape and the one §5 of the
reconstruction document is built around: *a check that finds nothing is not
the same as a check that found nothing wrong.*

Fixing it surfaced a second gap the Register did not mention: the same
defaults were hiding `scripts/car_library/vendor/`, so three findings in the
vendored ORPHEUS code had never been reported either. That directory is now
excluded **explicitly**, with the reason written down — vendoring is a
deliberate re-implementation, and its duplication is pinned by
`test_car_duration_tolerance_is_one_rule.py` rather than by this linter.

This test asserts the property, not the count. A count would have to be
updated every time a rule or a test changed, and would then be edited to
match reality rather than reporting on it.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_RULES = _ROOT / ".semgrep" / "rules.yml"
_IGNORE = _ROOT / ".semgrepignore"

needs_semgrep = pytest.mark.skipif(
    not shutil.which("semgrep"), reason="semgrep is a dev dependency"
)


def _scan(*paths: str) -> dict:
    """Run semgrep with the REAL home directory.

    conftest.py redirects HOME to a temporary directory before any import,
    so that config.py's _load_env() cannot leak the real credentials file
    into the suite. That is correct and stays. But semgrep is installed under
    ~/.local, so under the fake HOME its own launcher cannot import it:

        ModuleNotFoundError: No module named 'semgrep'

    and the call returns empty stdout, which reads as "no findings". A first
    draft of this file took that at face value -- a test about a scanner that
    silently scans nothing, itself silently scanning nothing.

    The real home comes from the password database rather than the
    environment, so it is unaffected by the redirect.
    """
    env = dict(os.environ)
    env["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
    proc = subprocess.run(
        ["semgrep", "--config", str(_RULES), *paths, "--quiet", "--json"],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        timeout=900,
        env=env,
    )
    if not proc.stdout.strip():
        raise AssertionError(
            f"semgrep produced no output (rc={proc.returncode}): {proc.stderr[:300]}"
        )
    return json.loads(proc.stdout)


def test_a_semgrepignore_exists_and_re_includes_tests() -> None:
    """Without this file, semgrep's defaults silently drop the tests tree."""
    assert _IGNORE.exists(), ".semgrepignore is gone; tests/ is unscanned again"
    body = _IGNORE.read_text()
    assert "!tests/" in body, "tests/ is no longer re-included"


def test_the_vendor_exclusion_is_explicit_not_accidental() -> None:
    """It was excluded by a default nobody chose. Now it is a decision."""
    body = _IGNORE.read_text()
    assert "scripts/car_library/vendor/" in body
    assert "vendoring IS a deliberate re-implementation" in body, (
        "the vendor exclusion has lost the reason it was made"
    )


@needs_semgrep
def test_the_documented_command_actually_scans_the_tests_tree() -> None:
    """The finding itself.

    Before the fix this scanned 136 files and none of them were tests.
    """
    scanned = _scan("musaeus/", "scripts/", "tests/")["paths"].get("scanned", [])
    from_tests = [p for p in scanned if p.startswith("tests/")]
    assert from_tests, (
        "the documented command scanned no test files -- a rule that claims "
        "to cover tests/ has examined it not once"
    )
    assert len(from_tests) > 50, len(from_tests)


@needs_semgrep
def test_the_defaults_alone_would_still_hide_the_tests_tree() -> None:
    """Pins WHY the file is needed, so nobody deletes it as redundant.

    Run with the ignore file moved aside, the same command goes blind again.
    If this ever stops being true -- semgrep changes its defaults -- the
    .semgrepignore can be reconsidered, and this test is what will say so.
    """
    stash = _IGNORE.with_suffix(".semgrepignore.pytest-stash")
    _IGNORE.rename(stash)
    try:
        scanned = _scan("tests/")["paths"].get("scanned", [])
    finally:
        stash.rename(_IGNORE)
    assert not scanned, (
        "semgrep's defaults no longer exclude tests/; the .semgrepignore may "
        "be simplified, but check the vendor exclusion still applies"
    )


@needs_semgrep
def test_the_tree_is_clean_under_real_coverage() -> None:
    """Clean *because it looked*, which is the whole point of the finding.

    Every remaining exception is an inline `# nosemgrep:` carrying a reason,
    so each is a recorded decision rather than an invisible gap.
    """
    results = _scan("musaeus/", "scripts/", "tests/").get("results", [])
    assert not results, [
        f"{r['path']}:{r['start']['line']} {r['check_id'].split('.')[-1]}" for r in results
    ]


def test_every_suppression_carries_a_reason() -> None:
    """A bare `# nosemgrep` is the gap re-opened one line at a time."""
    bare: list[str] = []
    for path in sorted((_ROOT / "tests").rglob("*.py")):
        if path == Path(__file__):
            continue  # this file discusses suppressions; it does not use them
        for i, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            # An actual suppression is a COMMENT. Prose that mentions the
            # word is not one -- a first draft of this check flagged its own
            # docstring, the fourth time in this session a text guard failed
            # to tell code from commentary.
            if not stripped.startswith("#") or "nosemgrep" not in stripped:
                continue
            after = stripped.split("nosemgrep", 1)[1]
            if ":" not in after or "--" not in after:
                bare.append(f"{path.relative_to(_ROOT)}:{i}")
    assert not bare, "nosemgrep without `: <rule-id> -- <reason>`: " + ", ".join(bare)
