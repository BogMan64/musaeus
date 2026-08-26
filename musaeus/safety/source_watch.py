#!/usr/bin/env python3
"""
MUSAEUS — Source drift detection.

A running Python process cannot pick up edits to its own source. Modules
are imported once and cached in sys.modules, so a console left open across
a fix keeps executing the code it loaded at startup.

This is not hypothetical. On 2026-08-25 a fix to ClassicalComposerStage
was written while a console launched 2h45m earlier was still open; the
next live run used the old code and stranded more files outside the vault.
The batch after that was launched into the same stale process.

Why re-exec and not importlib.reload
------------------------------------
DEFAULT_PIPELINE holds class OBJECTS, and instances, decorators and
closures hold references to the old ones. Reloading a module rebinds the
name but not the references already taken, so a reloaded pipeline is
half-old and half-new -- and silently so, which is the failure shape this
project keeps finding. Replacing the process gets a genuinely clean
interpreter and cannot be partially applied.

Content hash, not mtime
-----------------------
mtime changes when a file is touched, checked out, or copied without its
bytes changing, and would cry drift after every `git checkout`. The hash
answers the question actually being asked: is the code on disk different
from the code running?
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

# Set to "0"/"false"/"no" to keep a stale process (debugging, bisecting).
AUTO_RESTART_ENV = "MUSAEUS_AUTO_RESTART"

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _source_files(root: Path | None = None) -> list[Path]:
    root = root or _PACKAGE_ROOT
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def fingerprint(root: Path | None = None) -> str:
    """A content hash over every .py in the package."""
    h = hashlib.sha256()
    for p in _source_files(root):
        h.update(str(p).encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            # Unreadable now == different from what was read at startup.
            h.update(b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()


def changed_files(baseline: dict[Path, str], root: Path | None = None) -> list[Path]:
    """Which files differ from the per-file baseline (for the message)."""
    now = per_file(root)
    return sorted(set(baseline) ^ set(now) | {p for p in set(baseline) & set(now) if baseline[p] != now[p]})


def per_file(root: Path | None = None) -> dict[Path, str]:
    out: dict[Path, str] = {}
    for p in _source_files(root):
        try:
            out[p] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            out[p] = "<unreadable>"
    return out


def auto_restart_enabled() -> bool:
    return os.environ.get(AUTO_RESTART_ENV, "1").strip().lower() not in ("0", "false", "no")


class SourceWatch:
    """Records the source state at startup and reports drift from it."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or _PACKAGE_ROOT
        self.baseline_hash = fingerprint(self.root)
        self.baseline_files = per_file(self.root)

    def drifted(self) -> bool:
        return fingerprint(self.root) != self.baseline_hash

    def drifted_files(self) -> list[Path]:
        return changed_files(self.baseline_files, self.root)

    def restart(self) -> None:
        """Replace this process with a fresh interpreter. Does not return.

        Kept separate from detection so a caller can close its database
        connections first: exec drops the fds, and an open write
        transaction would be lost rather than committed.
        """
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable] + sys.argv)
