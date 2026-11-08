"""The findings from Kiro's review that survived a second pass.

Nineteen findings were left unverified after the seven confirmed P0s were
fixed. Going through them found most already fixed on this branch or purely
cosmetic. These are the ones that were real.

**P1-D — a hard reset left the resume file behind.** `musaeus reset` calls
`_clear_resume()`; the console's own hard reset did not. The resume file
records which stages have finished, keyed on stage NAME and nothing else, so
after wiping the database the next `musaeus run` read a file still saying
"ingest, sentinel, scholar: done" and skipped them against an empty
database — reporting success having processed nothing. A green result that
measured nothing, which is this project's signature failure.

**P1-K — a stalled ffmpeg killed the whole run.** `subprocess.TimeoutExpired`
inherits from `SubprocessError`, not `OSError`, so neither handler in
`_process_one` caught it. One hung file (600s for ffmpeg, 30s for ffprobe)
escaped, skipped `_quarantine_failed_staging`, and aborted the run — leaving
a half-written `.staging` file behind with nothing recording why. The
identical bug was fixed in `build_alac_library.py` on 2026-09-06; this copy
was missed.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


# ── P1-D ─────────────────────────────────────────────────────────────────────


def test_the_console_hard_reset_clears_the_resume_file() -> None:
    text = (_ROOT / "musaeus" / "console.py").read_text()
    assert "_clear_resume" in text, (
        "the console deletes the database without clearing the resume file; "
        "the next run will skip stages against an empty database (P1-D)"
    )


def test_the_clear_happens_before_the_console_stops() -> None:
    """It sets self._running = False right after. Order matters."""
    text = (_ROOT / "musaeus" / "console.py").read_text()
    assert text.index("_clear_resume()") < text.index("self._running = False")


def test_both_reset_paths_clear_it() -> None:
    """The CLI always did; the divergence is what made this a bug."""
    for module in ("cli.py", "console.py"):
        assert "_clear_resume" in (_ROOT / "musaeus" / module).read_text(), module


# ── P1-K ─────────────────────────────────────────────────────────────────────


def test_timeout_expired_is_not_an_oserror() -> None:
    """The premise. Both handlers caught OSError and missed this entirely."""
    assert not issubclass(subprocess.TimeoutExpired, OSError)
    assert issubclass(subprocess.TimeoutExpired, subprocess.SubprocessError)


def test_canonicalize_catches_a_stalled_subprocess() -> None:
    text = (_ROOT / "musaeus" / "stages" / "canonicalize.py").read_text()
    assert "subprocess.TimeoutExpired" in text, (
        "a stalled ffmpeg still aborts the whole stage (P1-K)"
    )


def test_the_timeout_handler_quarantines_like_the_others() -> None:
    """Catching without cleanup would leave the half-written staging file."""
    text = (_ROOT / "musaeus" / "stages" / "canonicalize.py").read_text()
    block = text.split("subprocess.TimeoutExpired")[1].split("except OSError")[0]
    assert "_quarantine_failed_staging" in block, (
        "the timeout is caught but its partial output is not cleaned up"
    )
    assert "return" in block, "the timeout must become one ERROR line, not a raise"


def test_the_timeout_handler_precedes_the_broad_one() -> None:
    """A later, broader `except` would shadow it and nothing would change."""
    tree = ast.parse((_ROOT / "musaeus" / "stages" / "canonicalize.py").read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        names = []
        for h in node.handlers:
            names.append(ast.unparse(h.type) if h.type else "bare")
        if any("TimeoutExpired" in n for n in names) and any(n == "OSError" for n in names):
            assert names.index(next(n for n in names if "TimeoutExpired" in n)) < names.index(
                "OSError"
            )
            return
    raise AssertionError("no try block handles both TimeoutExpired and OSError")
