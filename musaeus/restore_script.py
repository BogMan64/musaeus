"""Write standalone, non-destructive Python restore scripts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

PYTHON_RESTORE_HEADER = """#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
from pathlib import Path
import shutil


def display(path):
    return json.dumps(str(path))  # ASCII: an old Windows console cannot fail on it


def restore(source, destination):
    source_path = Path(source)
    destination_path = Path(destination)
    source_path.parent.mkdir(parents=True, exist_ok=True)
    if source_path.exists() or source_path.is_symlink():
        print(f\"skipped (source exists): {display(source_path)}\")
        return
    if not destination_path.exists():
        print(f\"skipped (destination missing): {display(destination_path)}\")
        return
    shutil.move(str(destination_path), str(source_path))
    print(f\"restored: {display(source_path)}\")

"""


def python_restore_call(source: str, destination: str) -> str:
    """Return one restore call with its paths as Python string literals (data)."""
    # repr, not json.dumps: JSON writes a character outside the BMP as two
    # escapes, which Python keeps as two lone surrogates (no such file).
    return f"restore({source!r}, {destination!r})\n"


def write_python_restore_script(path: Path, moves: Iterable[Mapping[str, str]]) -> None:
    """Write a complete Python restore script for ``moves``."""
    lines = [PYTHON_RESTORE_HEADER]
    lines.extend(python_restore_call(move["source"], move["destination"]) for move in moves)
    path.write_text("".join(lines), encoding="utf-8")
