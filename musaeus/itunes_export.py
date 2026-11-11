"""`musaeus export itunes`: the file the iTunes Companion reads.

The Companion (Grey's Windows front panel for iTunes, built by ChatGPT,
reviewed by Claude 2026-11-08/09) adds the lossless edition to iTunes and
mirrors its playlists. MUSAEUS is the authority; this writes what the
Companion is allowed to touch, in its `musaeus-itunes-bridge/v1` format:

    {"schema": "musaeus-itunes-bridge/v1",
     "library_root": "<the lossless edition>",
     "tracks": [{"path": ..., "previous_paths": [...]}],
     "playlists": [{"name": ..., "track_paths": [...]}]}

- tracks: every copy the edition ledger records for the lossless edition
  whose file is on disk. Never a master: the root is the edition, and the
  Companion refuses any path outside it.
- previous_paths: where the copy was before MUSAEUS refiled its master
  (ORGANIZE_MOVE / ORGANIZE_RENAME / ARTIST_CONSOLIDATED), mapped into the
  edition, so the Companion can re-link a moved song instead of adding a
  second one. A path another track holds now is left out.
- playlists: the edition's own `Playlists/*.m3u8` (genres, eras, All),
  entries limited to the tracks above.

Read-only: the catalogue and the ledger are opened read-only; the only
write is the export file.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .edition_build import EDITION, _mirror
from .edition_index import INDEX_DIRNAME
from .edition_ledger import ledger_path

SCHEMA = "musaeus-itunes-bridge/v1"
MOVE_EVENTS = ("ORGANIZE_MOVE", "ORGANIZE_RENAME", "ARTIST_CONSOLIDATED")


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _moves(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """new path -> the paths it was moved from, newest first. Read once: a
    query per song took five minutes on 7,991 songs."""
    qs = ",".join("?" * len(MOVE_EVENTS))
    out: dict[str, list[str]] = {}
    for old, new in conn.execute(
        f"SELECT old_value, new_value FROM events WHERE event_type IN ({qs}) ORDER BY id DESC",
        MOVE_EVENTS,
    ):
        if old and new:
            out.setdefault(new, []).append(old)
    return out


def _earlier_masters(moves: dict[str, list[str]], master: str) -> list[str]:
    """Every path *master* had before, newest first, following the move chain."""
    seen = {master}
    out: list[str] = []
    frontier = [master]
    while frontier:
        for old in moves.get(frontier.pop(), ()):
            if old not in seen:
                seen.add(old)
                out.append(old)
                frontier.append(old)
    return out


def _playlist_paths(playlist: Path, tracks: set[str]) -> list[str]:
    out: list[str] = []
    for line in playlist.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        path = os.path.normpath(playlist.parent / line)
        if path in tracks and path not in out:
            out.append(path)
    return out


def build(cfg: object) -> dict:
    """The bridge manifest for *cfg*'s lossless edition."""
    library = Path(cfg.alac_library)  # type: ignore[attr-defined]
    masters = Path(cfg.alac_archive)  # type: ignore[attr-defined]
    ledger = _read_only(ledger_path(cfg))
    try:
        rows = ledger.execute(
            "SELECT output_path, master_path FROM edition_copies WHERE edition = ? "
            "ORDER BY output_path",
            (EDITION,),
        ).fetchall()
    finally:
        ledger.close()
    copies = [
        (os.path.normpath(out), master)
        for out, master in rows
        if Path(out).is_file() and Path(out).is_relative_to(library)
    ]
    current = {out for out, _ in copies}

    catalogue = _read_only(Path(cfg.db_path))  # type: ignore[attr-defined]
    try:
        moves = _moves(catalogue)
    finally:
        catalogue.close()
    tracks = []
    for out, master in copies:
        previous: list[str] = []
        for old in _earlier_masters(moves, master):
            if not Path(old).is_relative_to(masters):
                continue  # it was not in the library then: no copy of it
            was = os.path.normpath(_mirror(masters, library, Path(old)))
            if was != out and was not in current and was not in previous:
                previous.append(was)
        tracks.append({"path": out, "previous_paths": previous})

    playlists = []
    index = library / INDEX_DIRNAME
    for pl in sorted(index.glob("*.m3u8")) if index.is_dir() else []:
        paths = _playlist_paths(pl, current)
        if paths:
            # The file name with spaces back: "Blues_Rock" -> "Blues Rock".
            playlists.append({"name": pl.stem.replace("_", " "), "track_paths": paths})

    return {
        "schema": SCHEMA,
        "library_root": str(library),
        "tracks": tracks,
        "playlists": playlists,
    }


def write(manifest: dict, out: Path) -> None:
    """Write *manifest* to *out*, whole or not at all."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, out)
