"""One duration tolerance, applied at write time and on resume alike.

M-03 in the Repair Register, 2026-09-08.

`build_aac_library.py` held three rules across two functions, with a comment
asserting that two of them agreed:

    _verify_bake            flat 2.0, no scaling
    _output_matches_source  inline max(1.0, src * 0.02)
    musaeus.duration        max(2.0, recorded * 0.02)

The consequence is a loop that reports success every time round. A 30-second
track whose encode drifts 1.4 s on AAC priming is **accepted at write time**
by the flat 2.0 and **rejected on the next run** by the 1.0 floor — so the
resume check deletes it and re-encodes it, and the next run does the same.
For ever, silently, logged as `CONVERTED`.

Two things made it invisible.

The comment at the resume check said it asked "the same thing `_verify_bake`
asks of a fresh encode", which was simply false — the kind of claim that
reads as verification and is decoration.

And the guarding test greps for `_DURATION_TOLERANCE_SEC\\s*=\\s*([0-9.]+)`.
It finds the 2.0 on its own line and passes. **An inline literal is
structurally invisible to it** — the constant was declared, respected in one
function, and quietly contradicted in the other.

The 1.0 floor appears in no ruling. The 2026-09-02 ruling settled 1.5-vs-2.0
at 2.0, and CLAUDE.md already lists this constant as recurring: "5 copies,
1.5 four times, 2.0 once, same stated rationale."

**Why a mirror rather than an import.** This file is vendored ORPHEUS code
and runs standalone — it imports `lib.orpheus_naming`, not `musaeus`. An
import would break the standalone property the vendoring exists to preserve.
So the rule is restated and pinned by the first test below, which is the only
thing keeping the two copies honest.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from musaeus.duration import TOLERANCE_SEC, tolerance_for

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "scripts" / "car_library" / "vendor"))
from build_aac_library import (  # noqa: E402
    _DURATION_TOLERANCE_SEC,
    _duration_tolerance,
    _output_matches_source,
)

_SCRIPT = (Path(__file__).resolve().parents[1]
           / "scripts" / "car_library" / "vendor" / "build_aac_library.py")


@pytest.mark.parametrize("recorded", [None, 0, -5, 0.5, 1, 30, 100, 300, 600, 3600])
def test_the_vendored_mirror_agrees_with_musaeus(recorded) -> None:
    """The only thing keeping a vendored copy from drifting.

    If someone changes musaeus.duration.tolerance_for and not this file, or
    the reverse, this fails — which is the entire reason the duplication is
    tolerable.
    """
    assert _duration_tolerance(recorded) == tolerance_for(recorded), recorded


def test_the_floor_is_the_ruled_value_not_one_second() -> None:
    """1.0 appears in no ruling; 2026-09-02 settled 1.5-vs-2.0 at 2.0."""
    assert _DURATION_TOLERANCE_SEC == TOLERANCE_SEC == 2.0
    assert _duration_tolerance(1) == 2.0


def test_the_tolerance_scales_with_length() -> None:
    """A flat 2 s is far too strict for a long track."""
    assert _duration_tolerance(600) == pytest.approx(12.0)
    assert _duration_tolerance(30) == 2.0


def test_no_inline_tolerance_literal_survives() -> None:
    """The shape the grep-based guard cannot see.

    An inline `max(1.0, ...)` is exactly what hid this: the constant was
    declared on its own line, found by the existing test, and contradicted a
    few hundred lines below.

    Parsed rather than grepped, and deliberately so — a first draft of this
    test searched the raw file and failed on the docstring of the very
    function that fixes the bug, which quotes the old expression to explain
    it. A guard that cannot tell code from prose is the same class of
    mistake as a guard that cannot see an inline literal.
    """
    tree = ast.parse(_SCRIPT.read_text())
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "max" and len(node.args) == 2):
            continue
        first, second = node.args
        # max(<number>, <something> * <something>) is a scaled tolerance
        if (isinstance(first, ast.Constant) and isinstance(first.value, (int, float))
                and isinstance(second, ast.BinOp) and isinstance(second.op, ast.Mult)):
            offenders.append((node.lineno, ast.unparse(node)))
    # The one inside _duration_tolerance() is the rule itself; any other is
    # a second opinion about the same question.
    allowed = {n.lineno for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_duration_tolerance"}
    stray = [o for o in offenders
             if not any(a <= o[0] <= a + 45 for a in allowed)]
    assert not stray, f"an inline duration tolerance is back: {stray}"


# ── the loop itself ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("recorded,drift", [
    (30, 1.4),     # the Register's case: AAC priming on a short track
    (30, 1.9),
    (600, 11.0),   # a long track, where the flat floor was far too strict
])
def test_a_drift_accepted_at_write_time_is_accepted_on_resume(recorded, drift) -> None:
    """The property that closes the loop, asserted directly on the rule.

    Anything _verify_bake would let through must also survive the resume
    check. If it does not, that file is deleted and re-encoded on every run
    for ever, and every run reports CONVERTED.
    """
    verify_accepts = drift <= _duration_tolerance(recorded)
    resume_accepts = drift <= _duration_tolerance(recorded)
    assert verify_accepts is resume_accepts is True


def test_a_real_truncation_is_still_rejected_by_both(tmp_path: Path) -> None:
    """Widening the tolerance must not swallow a genuinely short encode."""
    assert 15.0 > _duration_tolerance(300)   # a 5-minute track cut to 4:45
    assert 28.0 > _duration_tolerance(30)    # a 30 s track cut to 2 s


def test_the_resume_check_uses_the_shared_rule(tmp_path: Path) -> None:
    """Structural: the caller must reach the named rule, not re-inline one."""
    text = _SCRIPT.read_text()
    body = text.split("def _output_matches_source")[1].split("\ndef ")[0]
    assert "_duration_tolerance(" in body, \
        "the resume check no longer uses the shared tolerance"
