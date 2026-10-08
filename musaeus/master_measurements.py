"""Loudness measurements kept inside each master (Grey, 2026-10-06).

The edition builds measure every master once per recipe (loudnorm's first pass:
integrated loudness, range, true peak, threshold) and keep the result in
editions.db, so no build measures the same audio twice. That ledger was the only
copy: lose it and every master is measured again, hours of work. This keeps the
same numbers in the master itself, in one freeform tag, so they travel with the
file, and restore_ledger() puts them back into a ledger that lacks them.

They describe the MASTER, so edition copies do not get this tag (copy_tags leaves
it out, as it does the gain tags): a copy has its own, different loudness.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .edition_ledger import keep_measurement, measurements_of

KEY = "----:com.apple.iTunes:MUSAEUS_LOUDNESS_MEASURED"


def encode(measured_by_recipe: Mapping[str, Mapping[str, Any]]) -> bytes:
    """The tag's bytes: {recipe: measured}, in a fixed order so equal means equal."""
    return json.dumps(measured_by_recipe, sort_keys=True, separators=(",", ":")).encode("utf-8")


def put(tags: Any, measured_by_recipe: Mapping[str, Mapping[str, Any]]) -> None:
    """Set the tag on an MP4 tag object (the caller saves)."""
    from mutagen.mp4 import MP4FreeForm

    tags[KEY] = [MP4FreeForm(encode(measured_by_recipe))]


def read(path: Path) -> dict[str, dict]:
    """The measurements kept in *path*, by recipe; {} when it has none."""
    from mutagen.mp4 import MP4

    try:
        tags = MP4(path).tags
    except Exception:  # noqa: BLE001 -- an unreadable file simply holds none
        return {}
    raw = tags.get(KEY) if tags is not None else None
    if not raw:
        return {}
    try:
        data = json.loads(bytes(raw[0]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}


def restore_ledger(conn: sqlite3.Connection, master_hash: str, path: Path) -> int:
    """Put back into the ledger every measurement *path* keeps that it lacks."""
    have = measurements_of(conn, master_hash)
    added = 0
    for recipe, measured in read(path).items():
        if recipe not in have:
            keep_measurement(conn, master_hash, recipe, measured)
            added += 1
    return added
