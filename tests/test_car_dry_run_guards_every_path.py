"""--dry-run must stop every path, not only the catalogue one.

M-05 in the Repair Register, 2026-09-08.

`build_car_library.py`'s only dry-run check lived *inside*
`if args.from_catalogue:` — four spaces out, eight spaces in. A preview over
hand-dropped files took the `else` branch and ran for real. The help text
says "Report the plan and encode nothing" and names no dependency on another
flag; argparse enforced none.

What a `--dry-run` actually did in that mode, in order: prompted for noise
masking and blocked on stdin, computed an `audio_hash` of every input —
which is a full decode each — opened the database, encoded, and wrote at the
end. The Register estimates roughly 44 hours of ffmpeg. A safety flag that
performs the operation is worse than no flag, because the operator has been
told it is safe.

The same commit rejects `--limit` and `--budget-gb` outside
`--from-catalogue` instead of ignoring them. They only take effect during
selection, which happens in that branch; outside it, `--limit 5` encoded
everything. A silently dropped flag is worse than one that errors, because
the operator believes it took effect.

These tests read the shipped file rather than calling `main()`: `main()`
requires a configured vault, opens the database, and prompts on stdin, so a
test that exercised it would be testing the fixture. What matters is
structural and can be asserted structurally — the guard exists, and it comes
before anything that reads, decodes, prompts or writes.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "car_library" / "build_car_library.py"


def _main_body() -> list[ast.stmt]:
    tree = ast.parse(_SCRIPT.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node.body
    raise AssertionError("main() not found in build_car_library.py")


def _line_of_first(pattern: str) -> int:
    """1-indexed line of the first occurrence, or -1."""
    for i, line in enumerate(_SCRIPT.read_text().splitlines(), 1):
        if pattern in line:
            return i
    return -1


def test_the_dry_run_guard_is_not_nested_inside_the_catalogue_branch() -> None:
    """The defect itself: the guard must be reachable in BOTH modes.

    Walks main()'s top-level statements looking for an `if args.dry_run:`
    that is not inside `if args.from_catalogue:`. Before the fix there was
    none — every dry_run test sat one level deeper.
    """

    def is_dry_run_test(stmt: ast.stmt) -> bool:
        return (
            isinstance(stmt, ast.If)
            and isinstance(stmt.test, ast.Attribute)
            and stmt.test.attr == "dry_run"
        )

    top_level_guards = [s for s in _main_body() if is_dry_run_test(s)]
    assert top_level_guards, (
        "no top-level `if args.dry_run:` in main() — the only guard is nested "
        "inside a mode branch, which is M-05"
    )


def test_the_guard_returns_rather_than_falling_through() -> None:
    for stmt in _main_body():
        if (
            isinstance(stmt, ast.If)
            and isinstance(stmt.test, ast.Attribute)
            and stmt.test.attr == "dry_run"
        ):
            assert any(isinstance(s, ast.Return) for s in stmt.body), (
                "the dry-run guard must return, not merely print"
            )
            return
    pytest.fail("no top-level dry-run guard found")


def test_the_guard_precedes_the_masking_prompt_and_the_hashing() -> None:
    """Order is the whole point: it must stop BEFORE anything costly.

    The prompt blocks on stdin and audio_hash_safe decodes each file in full.
    A guard placed after either of them would still make --dry-run expensive
    and interactive.
    """
    guard = _line_of_first("if args.dry_run:")
    prompt = _line_of_first("Apply noise masking for car listening?")
    hashing = _line_of_first("audio_hash_safe(src)")

    assert guard > 0, "no dry-run guard in the shipped file"
    for name, line in (("the masking prompt", prompt), ("audio_hash_safe", hashing)):
        assert line > 0, f"{name} not found — this test needs updating"
        assert guard < line, f"the dry-run guard must come before {name}"


def test_limit_outside_catalogue_mode_is_refused_not_ignored() -> None:
    """Verified by running the real CLI, not by reading the source."""
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--dry-run", "--limit", "5"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode != 0, "--limit without --from-catalogue was accepted"
    assert "--from-catalogue" in proc.stderr, proc.stderr


def test_budget_gb_outside_catalogue_mode_is_refused_not_ignored() -> None:
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--dry-run", "--budget-gb", "30"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode != 0, "--budget-gb without --from-catalogue was accepted"
    assert "--from-catalogue" in proc.stderr, proc.stderr


def test_those_flags_are_still_accepted_in_catalogue_mode() -> None:
    """The rejection must not break the mode the flags exist for.

    Only the argument parsing is exercised: --help exits before main() does
    any work, so this cannot touch the vault.
    """
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--help"], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0
    for flag in ("--limit", "--budget-gb", "--dry-run", "--from-catalogue"):
        assert flag in proc.stdout, f"{flag} vanished from the interface"
