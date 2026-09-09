"""A failed probe must refuse the file, not encode it unpinned.

M-12 in the Repair Register, raised as PLAUSIBLE and confirmed by reading
2026-09-08.

`build_ffmpeg_command` emits both format flags *conditionally*:

    *(["-ar", str(target_rate)] if target_rate else [])
    *(["-ac", "2"] if (source_channels or 0) > 2 else [])

and both probes answer `None` on any ffprobe failure. So a failed probe does
not produce a *wrong* rate — it produces **no `-ar` at all**, which is worse,
because `car_sample_rate`'s own docstring says exactly what that costs:

    The rate must ALWAYS be stated: ffmpeg's loudnorm filter resamples
    internally and emits at its own rate, so an unpinned encode takes the
    FILTER's rate, not the source's. Measured 2026-08-31: a 44,100 Hz master
    came out as 96,000 Hz AAC.

`car_sample_rate(None)` returning `None` is correct on its own terms —
"unreadable: do not guess". The defect was the caller treating "do not
guess" as permission to proceed. It now refuses, and `convert_one` turns
that into one ERROR line for that file while the rest of the build carries
on.

**And the twin, M-08's other half.** Adding `timeout=` to those probes (this
morning) stopped them hanging, but none of them caught `TimeoutExpired` — so
a fired deadline became an uncaught exception that landed on `convert_one`'s
broad `except Exception` three frames away, where it reads as a mystery
rather than as an unreadable file. Each probe now answers with its own
documented "unreadable" value, which the M-12 guard above then refuses. The
two fixes meet at the same point on purpose: **one way for a file to be
unmeasurable, one response to it.**
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

_VENDOR = (Path(__file__).resolve().parents[1]
           / "scripts" / "car_library" / "vendor")
sys.path.insert(0, str(_VENDOR))
import build_aac_library as bal  # noqa: E402
from build_aac_library import (  # noqa: E402
    build_ffmpeg_command,
    car_sample_rate,
    probe_channels,
    probe_sample_rate,
)

_SCRIPT = _VENDOR / "build_aac_library.py"


# ── the shape of the hazard ──────────────────────────────────────────────────

def test_an_unreadable_source_yields_no_rate_at_all(tmp_path: Path) -> None:
    """Not a wrong rate -- an absent flag. That is why it was invisible."""
    junk = tmp_path / "junk.m4a"
    junk.write_text("not audio")
    assert probe_sample_rate(junk) is None
    assert probe_channels(junk) is None
    assert car_sample_rate(None) is None


def test_a_command_built_with_no_rate_omits_the_flag(tmp_path: Path) -> None:
    """Pins the mechanism, so the refusal upstream cannot be dropped."""
    cmd = build_ffmpeg_command(
        input_file=tmp_path / "in.m4a", output_file=tmp_path / "out.m4a",
        bitrate="256k", has_attached_picture=False, clean_tags={},
        loudnorm_filter="anull", target_rate=None, source_channels=None,
    )
    assert "-ar" not in cmd, "an unpinned command is exactly the hazard"

    pinned = build_ffmpeg_command(
        input_file=tmp_path / "in.m4a", output_file=tmp_path / "out.m4a",
        bitrate="256k", has_attached_picture=False, clean_tags={},
        loudnorm_filter="anull", target_rate=44_100, source_channels=2,
    )
    assert "-ar" in pinned and "44100" in pinned


# ── the refusal ──────────────────────────────────────────────────────────────

def test_convert_one_refuses_before_it_can_build_a_command() -> None:
    """Structural: the guard must precede the command build.

    Asserted on the shipped source because reaching this branch for real
    needs a staged file, a probe failure and a full encode path.
    """
    text = _SCRIPT.read_text()
    body = text.split("def convert_one")[1]
    guard = body.index("could not read the source sample rate")
    build = body.index("cmd = build_ffmpeg_command(")
    assert guard < build, "the refusal must come before the command is built"


def test_the_guard_checks_both_properties() -> None:
    text = _SCRIPT.read_text()
    # The whole function, not an arbitrary prefix -- a first draft sliced the
    # first 4,000 characters and missed a guard that is genuinely present,
    # which is a test reporting on its own window rather than on the code.
    body = text.split("def convert_one")[1].split("\ndef ")[0]
    assert "source_rate is None or source_channels is None" in body, \
        "both -ar and -ac vanish on a failed probe; both must be checked"


def test_the_probes_are_called_once_each_not_inline_twice() -> None:
    """They used to be called inside the argument list, so a reader could not
    see that a None there silently dropped a flag."""
    text = _SCRIPT.read_text()
    assert "target_rate=car_sample_rate(probe_sample_rate(file_path))" not in text


# ── M-08's other half ────────────────────────────────────────────────────────

@pytest.mark.parametrize("fn", ["probe_sample_rate", "probe_channels", "_probe_duration"])
def test_a_fired_deadline_answers_unreadable_not_an_exception(fn: str, monkeypatch,
                                                              tmp_path: Path) -> None:
    """A timeout must land on the function's own contract.

    Before this, adding timeout= turned a silent hang into an uncaught
    TimeoutExpired that surfaced three frames away as a generic ERROR.
    """
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd=["ffprobe"], timeout=30)

    monkeypatch.setattr(bal.subprocess, "run", _boom)
    f = tmp_path / "x.m4a"
    f.write_bytes(b"\x00" * 8)
    assert getattr(bal, fn)(f) is None, f"{fn} let the timeout escape"


@pytest.mark.parametrize("fn", ["probe_sample_rate", "probe_channels", "_probe_duration"])
def test_an_oserror_is_also_unreadable(fn: str, monkeypatch, tmp_path: Path) -> None:
    """ffprobe missing from PATH is an unreadable file, not a crash."""
    def _boom(*a, **k):
        raise OSError("ffprobe not found")

    monkeypatch.setattr(bal.subprocess, "run", _boom)
    f = tmp_path / "x.m4a"
    f.write_bytes(b"\x00" * 8)
    assert getattr(bal, fn)(f) is None


def test_every_probe_still_declares_its_deadline() -> None:
    """Catching the timeout must not tempt anyone to drop it."""
    tree = ast.parse(_SCRIPT.read_text())
    for name in ("probe_sample_rate", "probe_channels", "_probe_duration",
                 "_probe_rate_and_channels", "_probe"):
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                for call in [n for n in ast.walk(node)
                             if isinstance(n, ast.Call)
                             and getattr(n.func, "attr", "") == "run"]:
                    assert "timeout" in {k.arg for k in call.keywords}, name
                break
