"""Three small defects in the Car encoder, sharing one shape.

M-06, M-07 and M-08 in the Repair Register, 2026-09-08. Each is the same
mistake in a different register: **the code and the operator disagree about
what it does.**

**M-06 — the off switch turns it on.**
`FORCE_REENCODE = bool(os.environ.get("MUSAEUS_FORCE_REENCODE"))` tests
whether the variable is *set*, not what it says. `bool("0")` is True. So
setting it to `0`, `false` or `no` — the three spellings anyone reaches for
to disable an override — re-encoded all 10,545 files. The one spelling that
worked was unsetting the variable entirely.

**M-07 — the comment names a flag that does not exist.**
It read "`--force` re-encodes regardless". The script defines three
arguments and none is `--force`; following the comment produced an argparse
error, while the control that *does* exist went unnamed. A wrong instruction
is worse than none: it spends the operator's time proving the code wrong.

**M-08 — three probes dropped their sibling's timeout.**
`_probe` has carried `timeout=30` since it was written. `probe_sample_rate`,
`probe_channels` and `_probe_duration` passed none. ffprobe blocks
indefinitely on a truncated container — precisely this script's diet — and
with `MAX_WORKERS = 4`, four such files exhaust the pool and the build stops
with no output and no error. A hang is the worst failure mode available: it
looks like slow progress.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_VENDOR = (Path(__file__).resolve().parents[1]
           / "scripts" / "car_library" / "vendor")
sys.path.insert(0, str(_VENDOR))
from build_aac_library import _env_flag  # noqa: E402

_SCRIPT = _VENDOR / "build_aac_library.py"


# ── M-06 ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["0", "false", "False", "no", "NO", "off", ""])
def test_the_spellings_an_operator_uses_to_say_no_mean_no(value, monkeypatch) -> None:
    """The finding: every one of these used to mean YES."""
    monkeypatch.setenv("MUSAEUS_TEST_FLAG", value)
    assert _env_flag("MUSAEUS_TEST_FLAG") is False, value


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_the_spellings_that_mean_yes_still_mean_yes(value, monkeypatch) -> None:
    monkeypatch.setenv("MUSAEUS_TEST_FLAG", value)
    assert _env_flag("MUSAEUS_TEST_FLAG") is True, value


def test_an_unset_variable_is_off(monkeypatch) -> None:
    monkeypatch.delenv("MUSAEUS_TEST_FLAG", raising=False)
    assert _env_flag("MUSAEUS_TEST_FLAG") is False


def test_whitespace_does_not_flip_the_answer(monkeypatch) -> None:
    monkeypatch.setenv("MUSAEUS_TEST_FLAG", "  0  ")
    assert _env_flag("MUSAEUS_TEST_FLAG") is False


def test_the_force_flag_reads_its_value_not_its_presence() -> None:
    """Structural: the module-level constant must go through _env_flag.

    Reassigning it at runtime would not help -- it is read at import.
    """
    tree = ast.parse(_SCRIPT.read_text())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "FORCE_REENCODE"):
            src = ast.unparse(node.value)
            assert "_env_flag" in src, src
            assert "bool(" not in src, f"presence test is back: {src}"
            return
    pytest.fail("FORCE_REENCODE is no longer assigned at module level")


# ── M-07 ─────────────────────────────────────────────────────────────────────

def test_no_comment_promises_a_flag_the_script_does_not_define() -> None:
    """A wrong instruction costs more than a missing one.

    Parses the file for the arguments it really defines, then checks that no
    comment sends the operator to one that is absent.
    """
    text = _SCRIPT.read_text()
    tree = ast.parse(text)
    defined = {
        a.value for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", "") == "add_argument"
        for a in node.args
        if isinstance(a, ast.Constant) and isinstance(a.value, str)
        and a.value.startswith("--")
    }
    promised: list[str] = []
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        for word in stripped.replace("`", " ").split():
            flag = word.strip(".,;:()")
            if flag.startswith("--") and len(flag) > 2 and flag not in defined:
                promised.append(f"line {i}: {flag}")
    assert not promised, (
        "a comment names a flag this script does not define: "
        + ", ".join(promised))


# ── M-08 ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fn", [
    "_probe", "probe_sample_rate", "probe_channels",
    "_probe_duration", "_probe_rate_and_channels",
])
def test_every_ffprobe_helper_has_a_deadline(fn: str) -> None:
    """ffprobe blocks for ever on a truncated container.

    With MAX_WORKERS = 4, four such files exhaust the pool and the build
    stalls silently. Asserted per-helper so a new one cannot be added
    without one.
    """
    tree = ast.parse(_SCRIPT.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != fn:
            continue
        runs = [
            n for n in ast.walk(node)
            if isinstance(n, ast.Call)
            and getattr(n.func, "attr", "") == "run"
        ]
        assert runs, f"{fn} no longer calls subprocess.run"
        for call in runs:
            kwargs = {k.arg for k in call.keywords}
            assert "timeout" in kwargs, f"{fn} calls subprocess.run with no timeout"
        return
    pytest.fail(f"{fn} not found")


def test_the_probe_timeout_is_named_once() -> None:
    text = _SCRIPT.read_text()
    assert "_PROBE_TIMEOUT_SEC = 30" in text
    assert text.count("timeout=_PROBE_TIMEOUT_SEC") >= 4
